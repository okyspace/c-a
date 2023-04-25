"""
Create a ClearML Monitoring Service that posts alerts on Slack Channel groups based on some logic

Creating a new Slack Bot (Allegro ClearML Bot):
1. Login to your Slack account
2. Go to https://api.slack.com/apps/new
3. Give the new App a name (For example "Allegro Train Bot") and select your workspace
4. Press Create App
5. In "Basic Information" under "Display Information" fill in the following fields
    - In "Short description" insert "Allegro Train Bot"
    - In "Background color" insert #202432
6. Press Save Changes
7. In "OAuth & Permissions" under "Scopes" click on "Add an OAuth Scope" and
   select from the drop down list the following three permissions:
        channels:join
        channels:read
        chat:write
8. Now under "OAuth Tokens & Redirect URLs" press on "Install App to Workspace",
   then hit "Allow" on the confirmation dialog
9. Under "OAuth Tokens & Redirect URLs" copy the "Bot User OAuth Access Token" by clicking on "Copy" button
10. To use the copied API Token in the Allegro ClearML Slack service,
    execute the script with --slack_api "<api_token_here>"  (notice the use of double quotes around the token)

We are done!
"""

import argparse
import hashlib
import logging
import os
from datetime import datetime
from time import sleep, time
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from clearml import Task
from clearml.automation.monitor import Monitor
from slack import WebClient
from slack.errors import SlackApiError

from usage_reporter import UsageReporter
from clearml.backend_api.session import Session

reporting_units = 1

class ProjectMonitor(Monitor):

    """
    Create a monitoring service that alerts on Task failures / completion in a Slack channel
    """
    def __init__(self, slack_api_token, channel, message_prefix=None):
        # type: (str, str, Optional[str]) -> ()
        """
        Create a Slack Monitoring object.
        It will alert on any Task/Experiment that failed or completed

        :param slack_api_token: Slack bot API Token. Token should start with "xoxb-"
        :param channel: Name of the channel to post alerts to
        :param message_prefix: optional message prefix to add before any message posted
            For example: message_prefix="Hey <!here>,"
        """
        super(ProjectMonitor, self).__init__()

        self.min_num_iterations = 0
        self.status_alerts = ["failed", ]
        self.include_manual_experiments = False
        self._channel_id = None
        self._message_prefix = '{} '.format(message_prefix) if message_prefix else ''
        self._titles = None
        self._series = None
        self._maxmin = None
        self._monitor_task = None
        self._iteration = 0
        self._active_worker_list = []
        self._total_gpu_mem = 0
        self._total_gpu_util = 0
        self._total_failed_tasks = 0
        self._failed_task_list_len = 5
        self._failed_task_list = [ ['NA'] * self._failed_task_list_len for i in range(2)]

        if channel and slack_api_token:
            self.channel = '{}'.format(channel[1:] if channel[0] == '#' else channel)
            self.slack_client = WebClient(token=slack_api_token)
            self.check_credentials()
        else:
            self.slack_client = None

    def check_credentials(self):
        # type: () -> ()
        """
        Check we have the correct credentials for the slack channel
        """
        self.slack_client.api_test()

        # Find channel ID
        response = self.slack_client.conversations_list()
        channel_id = [channel_info.get('id') for channel_info in response.data['channels']
                      if channel_info.get('name') == self.channel]
        if not channel_id:
            raise ValueError('Error: Could not locate channel name \'{}\''.format(self.channel))

        # test bot permission (join channel)
        self._channel_id = channel_id[0]
        self.slack_client.conversations_join(channel=self._channel_id)

    def post_message(self, message, retries=1, wait_period=10.):
        # type: (str, int, float) -> ()
        """
        Post message on our slack channel

        :param message: Message to be sent (markdown style)
        :param retries: Number of retries before giving up
        :param wait_period: wait between retries in seconds
        """
        for i in range(retries):
            if i != 0:
                sleep(wait_period)

            try:
                self.slack_client.chat_postMessage(
                    channel=self._channel_id,
                    blocks=[dict(type="section", text={"type": "mrkdwn", "text": message})],
                )
                return
            except SlackApiError as e:
                print("While trying to send message: \"\n{}\n\"\nGot an error: {}".format(
                    message, e.response['error']))

    def get_query_parameters(self):
        # type: () -> dict
        """
        Return the query parameters for the monitoring.

        :return dict: Example dictionary: {'status': ['failed'], 'order_by': ['-last_update']}
        """
        filter_tags = ['-archived'] + (['-development'] if not self.include_manual_experiments else [])
        return dict(status=self.status_alerts, order_by=['-last_update'], system_tags=filter_tags)

    def check_failed_task(self, task):
        """
        # type: (Task) -> ()
        Called on every Task that we monitor.
        This is where we send the Slack alert

        :return: None
        """
        # skipping failed tasks with low number of iterations
        if self.min_num_iterations and task.get_last_iteration() < self.min_num_iterations:
            print('Skipping {} experiment id={}, number of iterations {} < {}'.format(
                task.status, task.id, task.get_last_iteration(), self.min_num_iterations))
            return

        print('Experiment id={} {}, raising alert on channel \"{}\"'.format(task.id, task.status, self.channel))
        print(task.get_output_log_web_page())
        self._failed_task_list[0][self._total_failed_tasks % self._failed_task_list_len] = '<a href="{}"> {} </a>'.format(
                    task.get_output_log_web_page(), task.name)
        self._failed_task_list[1][self._total_failed_tasks % self._failed_task_list_len] = datetime.now()

        self._total_failed_tasks += 1
        self._monitor_task.set_parameter(name='General/total_failed', value=self._total_failed_tasks)
        console_output = task.get_reported_console_output(number_of_reports=3)
        message = \
            '{}Experiment ID <{}|{}> *{}*\nProject: *{}*  -  Name: *{}*\n' \
            '```\n{}\n```'.format(
                self._message_prefix,
                task.get_output_log_web_page(), task.id,
                task.status,
                task.get_project_name(), task.name,
                ('\n'.join(console_output))[-2048:])
        self.post_message(message, retries=5)

    def set_metrics(self, titles=None, series=None, maxmin=None):
        self._titles = titles
        self._series = series
        self._maxmin = maxmin.lower()

    def set_task(self, task=None):
        self._monitor_task = task

    def get_last_metric(self, task_list):
        hashed_title = hashlib.md5(str(self._titles).encode('utf-8')).hexdigest()
        hashed_series = hashlib.md5(str(self._series).encode('utf-8')).hexdigest()

        if task_list:
            if self._maxmin == 'highest':
                return max(
                    [getattr(task, 'last_metrics').get(hashed_title, {}).get(hashed_series, {}).get('max_value',
                                                                                                    -1) for
                     task in
                     task_list])
            else:
                return min(
                    [getattr(task, 'last_metrics').get(hashed_title, {}).get(hashed_series, {}).get('min_value',
                                                                                                    -1) for
                     task in
                     task_list])

    def get_task_statistics(self, task_list):
        gpu_params = hashlib.md5(str(':monitor:gpu').encode('utf-8')).hexdigest()
        gpu_mem = hashlib.md5(str('gpu_0_mem_usage').encode('utf-8')).hexdigest()
        gpu_util = hashlib.md5(str('gpu_0_utilization').encode('utf-8')).hexdigest()

        task_stat_dict = {'completed': 0, 'published': 0, 'aborted': 0,
                          'running': 0, 'failed': 0,
                          'draft': 0, 'pending': 0,
                          'dev': 0, 'worker': 0}

        self._total_gpu_mem = 0
        self._total_gpu_util = 0
        self._active_worker_list = []
        for task in task_list:
            if task.status.value == 'stopped':
                task_stat_dict['aborted'] += 1
            elif task.status.value == 'completed':
                task_stat_dict['completed'] += 1
            elif task.status.value == 'published':
                task_stat_dict['published'] += 1
            elif task.status.value == 'created':
                task_stat_dict['draft'] += 1
            elif task.status.value == 'queued':
                task_stat_dict['pending'] += 1
            elif task.status.value == 'in_progress':
                task_stat_dict['running'] += 1
                # Supports only 1 gpu
                if task.last_metrics:
                    self._total_gpu_mem += task.last_metrics[gpu_params][gpu_mem]['value']
                    self._total_gpu_util += task.last_metrics[gpu_params][gpu_util]['value']
                if task.last_worker:
                    self._active_worker_list.append(task.last_worker)
            elif task.status.value == 'failed':
                task_stat_dict['failed'] += 1

            if 'development' in task.system_tags:
                task_stat_dict['dev'] += 1
            else:
                task_stat_dict['worker'] += 1

        return task_stat_dict

    def report_plots(self, task_list, task_dict, title, series):

        workers_list = self._get_api_client().workers.get_all()
        metric = self.get_last_metric(task_list)

        # Reorganize data for plotly graphs
        status_data = {
            "type": [
                'Completed',
                'Published',
                'Running',
                'Draft',
                'Pending',
                'Failed',
                'Aborted'
            ],
            "count": [
                task_dict['completed'],
                task_dict['published'],
                task_dict['running'],
                task_dict['draft'],
                task_dict['pending'],
                task_dict['failed'],
                task_dict['aborted']
            ]
        }
        
        status_colors = {
            "Completed": '#009AFF',
            "Published": '#CAFF00',
            "Running": '#50E3C3',
            "Failed": '#FF001F',
            "Aborted": '#4A90E2', 
            "Draft": '#5A658E', 
            "Pending": '#7480AA'
        }
        
        tag_data = {
            "type": ['Manual', 'Worker'],
            "count": [task_dict['dev'], task_dict['worker']]
        }
        
        tag_colors = {
            "Manual": '#5A658E',
            "Worker": '#8F9DC9'
        }

        # Creating Plotly Graphs
        status_df = pd.DataFrame(status_data)
        status_fig = px.pie(status_df, values='count', names='type', color='type', color_discrete_map=status_colors)
        status_fig.update_traces(textposition='inside',
                                 textinfo='percent+label',
                                 marker=dict(line=dict(color='#0d0e15', width=1)))
        status_fig.update(layout_showlegend=False)
        status_fig.update_layout(hoverlabel_bgcolor='#4D66FF')

        tag_df = pd.DataFrame(tag_data)
        tag_fig = px.pie(tag_df, values='count', names='type', color='type', color_discrete_map=tag_colors)
        tag_fig.update_traces(textposition='inside',
                              textinfo='percent+label',
                              marker=dict(line=dict(color='#0d0e15', width=1)))
        tag_fig.update(layout_showlegend=False)
        tag_fig.update_layout(hoverlabel_bgcolor='#4D66FF')
        
        running_gauge = go.Figure(go.Indicator(
            domain={'x': [0, 1], 'y': [0, 1]},
            value=task_dict['running'],
            mode="gauge+number",
            gauge={'bar': {'color': "#69DB36"},
                   'bordercolor': '#39405F',
                   'axis': {'range': [None, len(task_list)], 'tickcolor': '#39405F'},
                   'bgcolor': 'rgba(0,0,0,0)'}
        ))
        running_gauge.update_layout(font={'color': "#C1CDF3"})

        # TODO change this to max number of workers available excluding services
        workers_gauge = go.Figure(go.Indicator(
            domain={'x': [0, 1], 'y': [0, 1]},
            value=len(self._active_worker_list),
            mode="gauge+number",
            gauge={'bar': {'color': "#8F9DC9"},
                   'bordercolor': '#39405F',
                   'axis': {'range': [None, len(workers_list)], 'tickcolor': '#39405F'},
                   'bgcolor': 'rgba(0,0,0,0)'}
        ))
        workers_gauge.update_layout(font={'color': "#C1CDF3"})

        worker_table = go.Figure(data=[go.Table(header=dict(line_color='#39405F',
                                                            fill_color='#202432',
                                                            font=dict(color='#C1CDF3', size=14)),
                                                cells=dict(values=[self._active_worker_list],
                                                           line_color='#39405F',
                                                           fill_color='rgba(0,0,0,0)',
                                                           font=dict(color='#C1CDF3', size=12)))])

        failed_task_table = go.Figure(data=[go.Table(header=dict(values=['Task Name', 'Fail Time']),
                                       cells=dict(values=self._failed_task_list))
                              ])



        logger = self._monitor_task.get_logger()
        logger.report_plotly(title="task info", series="Status", iteration=0, figure=status_fig)
        logger.report_plotly(title="task info", series="Tag", iteration=0, figure=tag_fig)
        logger.report_plotly(title="Running Experiments", series="", iteration=0, figure=running_gauge)
        logger.report_plotly(title="Active Workers", series="Workers", iteration=0, figure=workers_gauge)
        logger.report_plotly(title="Active Workers", series="Active Workers", iteration=0, figure=worker_table)
        logger.report_plotly(title="Failed Tasks", series="Tasks", iteration=0, figure=failed_task_table)

        logger.report_scalar(title, series, iteration=self._iteration, value=metric)
        logger.report_scalar('Experiments', 'Total', iteration=self._iteration, value=len(task_list))
        logger.report_scalar('Experiments', 'Running', iteration=self._iteration, value=task_dict['running'])
        logger.report_scalar('Experiments', 'Completed', iteration=self._iteration, value=task_dict['completed'])
        logger.report_scalar('Experiments', 'Published', iteration=self._iteration, value=task_dict['published'])
        logger.report_scalar('Experiments', 'Aborted', iteration=self._iteration, value=task_dict['aborted'])
        logger.report_scalar('Experiments', 'Failed', iteration=self._iteration, value=task_dict['failed'])
        logger.report_scalar('Experiments', 'Draft', iteration=self._iteration, value=task_dict['draft'])
        logger.report_scalar('Experiments', 'Pending', iteration=self._iteration, value=task_dict['pending'])
        logger.report_scalar('Monitoring', 'Total GPU Memory Usage', iteration=self._iteration,
                             value=self._total_gpu_mem)
        logger.report_scalar('Monitoring', 'Total GPU Utilization', iteration=self._iteration,
                             value=self._total_gpu_util)
        self._iteration += 1

    def monitor_step(self):
        # type: () -> ()
        """
        Overrides parent method
        """

        previous_timestamp = self._previous_timestamp or time()
        timestamp = time()
        try:
            # TODO support more than a single metric
            # hashed_title = hashlib.md5(str(self._titles).encode('utf-8')).hexdigest()
            # hashed_series = hashlib.md5(str(self._series).encode('utf-8')).hexdigest()
            # sign = '.max_value' if self._ismax else '.min_value'

            # filter = ['id', 'last_metrics.' + hashed_title + '.' + hashed_series + sign, 'status','system_tags',
            # 'last_worker', 'last_metrics.' + gpu_params + '.' + gpu_mem + '.value']
            # TODO We go over ALL experiments always. Need to only go through recently updated ones
            # TODO Get only information we need, not entire Task's info

            task_list = self._get_api_client().tasks.get_all(project=self._get_projects_ids(),
                                                             system_tags=['-archived'])

            stat_dict = self.get_task_statistics(task_list)
            self.report_plots(task_list, stat_dict, self._titles, self._series)

        except Exception as ex:
            # do not update the previous timestamp
            print('Exception querying Tasks: {}'.format(ex))
            return

        # Todo - This is redundant. We should unify the 2 calls.
        # Check if we need to report to slack
        if self.slack_client:
            # Getting all Tasks updated in last refresh seconds.
            task_filter = self.get_query_parameters()
            task_filter.update(
                {
                    'page_size': 100,
                    'page': 0,
                    'status_changed': ['>{}'.format(datetime.utcfromtimestamp(previous_timestamp)), ],
                    'project': self._get_projects_ids(),
                }
            )

            queried_tasks = Task.get_tasks(task_name=self._task_name_filter, task_filter=task_filter)
            # process queried tasks
            for task in queried_tasks:
                try:
                    self.check_failed_task(task)
                except Exception as ex:
                    print('Exception processing Task ID={}:\n{}'.format(task.id, ex))

        self._previous_timestamp = timestamp


def main():

    # Slack Monitor arguments
    parser = argparse.ArgumentParser(description='ClearML monitor experiments and post Slack Alerts')
    parser.add_argument('--channel', type=str,
                        help='Set the channel to post the Slack alerts')
    parser.add_argument('--slack_api', type=str, default=os.environ.get('SLACK_API_TOKEN', None),
                        help='Slack API key for sending messages')
    parser.add_argument('--message_prefix', type=str,
                        help='Add message prefix (For example, to alert all channel members use: "Hey <!here>,")')
    parser.add_argument('--project', type=str, default='',
                        help='The name (or partial name) of the project to monitor, use empty for all projects')
    parser.add_argument('--series', type=str, default='',
                        help='Metrics series')
    parser.add_argument('--title', type=str, default='',
                        help='Metrics titles')
    parser.add_argument('--maxmin', type=str, default='highest',
                        help='Whether to check minimum or maximum of tracked metric')
    parser.add_argument('--min_num_iterations', type=int, default=0,
                        help='Minimum number of iterations of failed/completed experiment to alert. '
                             'This will help eliminate unnecessary debug sessions that crashed right after starting '
                             '(default:0 alert on all)')
    parser.add_argument('--include_manual_experiments', action="store_true", default=True,
                        help='Include experiments running manually (i.e. not by clearml-agent)')
    parser.add_argument('--include_completed_experiments', action="store_true", default=False,
                        help='Include completed experiments (i.e. not just failed experiments)')
    parser.add_argument('--refresh_rate', type=float, default=60,
                        help='Set refresh rate of the monitoring service, default every 60.0 sec')
    args = parser.parse_args()

    # Ensure that if we receive slack_api we also get slack channel
    if bool(args.slack_api) ^ bool(args.channel):
        print('You must provide Slack API key and Channel')
        exit(1)

    report_to_slack = True if args.slack_api else False

    # create the slack monitoring object
    proj_monitor = ProjectMonitor(
        slack_api_token=args.slack_api, channel=args.channel, message_prefix=args.message_prefix,
        )

    proj_monitor.set_projects(project_names=[args.project])

    proj_monitor.set_metrics(titles=args.title, series=args.series, maxmin=args.maxmin)
    # TODO - Make this configurable externally?
    if args.include_completed_experiments:
        proj_monitor.status_alerts += ["completed"]

    task = Task.init(project_name='Monitoring', task_name='Project_Dashboard', task_type=Task.TaskTypes.monitor,
                     auto_resource_monitoring=False)
    session: Session = task.session

    reporter = UsageReporter(session=session, app_id="project-dashboard", report_interval_sec=60,units = max(1, reporting_units))

    proj_monitor.set_task(task)

    print('\nStarting monitoring service\nProject: "{}"\nRefresh rate: {}s\n'.format(
        args.project or 'all', args.refresh_rate))

    if report_to_slack:
        # configure the monitoring filters
        proj_monitor.min_num_iterations = args.min_num_iterations
        proj_monitor.include_manual_experiments = args.include_manual_experiments
        # Let everyone know we are up and running
        start_message = \
            '{}Allegro ClearML Slack monitoring service started\nMonitoring project \'{}\''.format(
                (args.message_prefix + ' ') if args.message_prefix else '',
                args.project or 'all')
        proj_monitor.post_message(start_message)

    # Start the monitor service, this function will never end
    proj_monitor.monitor(pool_period=args.refresh_rate)


if __name__ == "__main__":
    main()




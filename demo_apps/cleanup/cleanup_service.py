import logging
from datetime import datetime
from time import sleep, time

from clearml import Task
import plotly.graph_objects as go
import json

# Connecting ClearML with the current process,
# from here on everything is logged automatically

task = Task.init(
    project_name="DevOps",
    task_name="Cleanup Service",
    task_type=Task.TaskTypes.service,
    reuse_last_task_id=False,
)

logger = task.get_logger()

# set the base docker including the mount point for the file server data data
file_server_mount = "/opt/clearml/data/fileserver/"
task.set_base_docker(
    "ubuntu:18.04 -v /opt/clearml/data/fileserver/:{}".format(file_server_mount)
)

# args for the running task
args = {
    "delete_threshold_days": 0.0001,
    "cleanup_period_in_days": 0.00002,
    "delete_artifacts": False,
    "debug": True
}
args = task.connect(args)
proj_cfg = task.connect_configuration('project_list.json', name='project_list')
proj_list = json.load(open(proj_cfg, 'rt'))
print("Cleanup service started")

total_deleted = 0
total_last_deleted = 0
task.set_parameter(name='General/total_del',value=total_deleted)
task.set_parameter(name='General/total_last_del', value=total_last_deleted)
print("Starting cleanup")

while True:
    print("Deleting Old Experiments")
    # anything that has not changed in the last month
    timestamp = time() - 60 * 60 * 24 * args["delete_threshold_days"]
    total_last_deleted = 0
    projects = []
    if len(proj_list) == 0:
        projects = None
    else:
        projects = [p['project_name'] for p in proj_list]

    tasks = Task.get_tasks(project_name=projects, task_filter={'system_tags':["archived"],'_allow_extra_fields_': True,
                                                             'order_by':['-last_update'],'last_update':['<{}'.format(datetime.utcfromtimestamp(timestamp))]})

    # delete and cleanup tasks
    for t in tasks:
        # noinspection PyBroadException
        try:
            #Delete Task
            if args["debug"]:
                print(t.name)
            else:
                t.delete(delete_artifacts_and_models=args['delete_artifacts'],skip_models_used_by_other_tasks=True)

            total_deleted += 1
            total_last_deleted += 1

        except Exception as ex:
            logging.warning(
                "Could not delete Task ID={}, {}".format(
                    task.id, ex.message if hasattr(ex, "message") else ex
                )
            )
            continue


    total_deleted_fig =\
              go.Figure(go.Indicator(
              mode="number",
              value=total_deleted,
              domain={'x': [0, 1], 'y': [0, 1]},
              ))

    last_deleted_fig = \
             go.Figure(go.Indicator(
             mode="number",
             value=total_last_deleted,
             domain={'x': [0, 1], 'y': [0, 1]},
             ))
    logger.report_plotly(title='Total Deleted',series='',figure=total_deleted_fig)
    logger.report_plotly(title='Deleted in the Last Iteration',series='', figure=last_deleted_fig)

    print('Total tasks deleted: ' + str(total_deleted) + ' Tasks deleted in last iteration: ' + str(total_last_deleted))

    # sleep until the next day
    print("going to sleep for {} days".format(args["cleanup_period_in_days"]))
    sleep(60 * 60 * 24.0 * args["cleanup_period_in_days"])

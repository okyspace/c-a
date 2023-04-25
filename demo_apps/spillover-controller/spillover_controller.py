import json
import logging
from collections import defaultdict
from math import ceil
from time import sleep, time
from typing import Dict, List

import plotly.graph_objects as go
from attr import attrs, attrib

from clearml import Task
from clearml.backend_api.services import tasks as tasks_service
from clearml.backend_api.session.client import APIClient
from clearml.backend_interface.util import exact_match_regex


class SpilloverController(object):
    definitions = {
        "monitor_queue_name": None,
        "monitor_queue_id": None,
    }

    class AsDict(object):
        def get(self, key, default=None):
            return getattr(self, key, default)

        def update(self, a_dict):
            for k, v in a_dict.items():
                self[k] = v

        def __getitem__(self, item):
            return self.get(item)

        def __setitem__(self, item, value):
            return setattr(self, item, value)

    @attrs
    class Definitions(AsDict):
        monitor_queue_name = attrib(type=str, default=None)
        monitor_queue_id = attrib(type=str, default=None)

    @attrs
    class QueueStat(AsDict):
        name = attrib(type=str, default=None)
        max_jobs = attrib(type=int, default=0)
        running = attrib(type=int, default=0)

    @attrs
    class QueueDefinition(AsDict):
        name = attrib(type=str, default=None)
        id = attrib(type=str, default=None)
        max_jobs = attrib(type=int, default=0)

    def __init__(self, k8s_pending_queue="k8s_scheduler"):
        self.execution_queues = []  # type: List[SpilloverController.QueueDefinition]
        self.k8s_pending_queue = k8s_pending_queue
        self.k8s_pending_queue_id = None
        self.monitored_queue_name = None
        self.monitored_queue_id = None
        self.queue_statistics = dict()  # type: Dict[str, SpilloverController.QueueStat]
        self.current_running_jobs = 0
        self.max_running_jobs = 0
        self.last_config_refresh_ts = 0
        self.task = None
        self.client = None
        self.initial_definitions = self.Definitions()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.addHandler(logging.StreamHandler())
        self.logger.setLevel(logging.DEBUG)
        self.report_iteration = 0

    def update_statistics(self):
        max_per_line = 4
        entries = 1 + len(self.queue_statistics)
        rows = ceil(entries / float(max_per_line))
        cols = min(max_per_line, entries)
        fig = go.Figure()
        fig.add_trace(
            go.Indicator(
                domain={"row": 0, "column": 0},
                title={"text": "total jobs"},
                value=self.current_running_jobs,
                mode="gauge+number",
                gauge={
                    "bar": {"color": "#69DB36"},
                    "bordercolor": "#39405F",
                    "steps": [
                        {"range": [0, self.max_running_jobs], "color": "lightgray"},
                    ],
                    "axis": {
                        "range": [None, self.max_running_jobs],
                        "tickcolor": "#39405F",
                        "dtick": max(1, self.max_running_jobs // 4),
                    },
                    "bgcolor": "rgba(0,0,0,0)",
                },
            )
        )

        for i, (q_id, q) in enumerate(self.queue_statistics.items()):
            fig.add_trace(
                go.Indicator(
                    domain={"row": (i + 1) // cols, "column": (i + 1) % cols},
                    title={"text": "'{}' jobs".format(q["name"])},
                    value=q["running"],
                    mode="gauge+number",
                    gauge={
                        "bar": {"color": "#69DB36"},
                        "steps": [
                            {"range": [0, q["max_jobs"]], "color": "lightgray"},
                        ],
                        "bordercolor": "#39405F",
                        "axis": {
                            "range": [None, q["max_jobs"]],
                            "tickcolor": "#39405F",
                            "dtick": max(1, q["max_jobs"] // 4),
                        },
                        "bgcolor": "rgba(0,0,0,0)",
                    },
                )
            )

        fig.update_layout(
            font={"color": "#C1CDF3"},
            grid={"rows": rows, "columns": cols, "pattern": "independent"},
            template={"data": {"indicator": [{"title": {"text": "Speed"}, "mode": "number+gauge"}]}},
        )
        # TODO: remove this once the UI knows how to refresh the display when receiving a new plot with
        #  a previously reported iteration number
        self.report_iteration += 1
        self.task.get_logger().report_plotly(
            title="jobs", series="running", figure=fig, iteration=self.report_iteration
        )

    def _get_queue_id(self, queue_name, raise_=False):
        if not queue_name:
            if raise_:
                raise ValueError("Failed resolving queue ID (empty queue name)")
            return None
        result = self.client.queues.get_all(name=exact_match_regex(queue_name))
        if not result:
            if raise_:
                raise ValueError("Failed resolving queue ID for {}".format(queue_name))
            return None
        return result[0].id

    def refresh_configuration(self, refresh_interval_minutes=15):
        # if X minutes passed, refresh our internal state based on the configuration object
        # (allow for realtime update)
        if (time() - self.last_config_refresh_ts) // 60 < refresh_interval_minutes:
            # no need to refresh configuration
            return

        self.last_config_refresh_ts = time()
        self.logger.info("Reloading configuration")

        definitions = self.initial_definitions or self.Definitions()
        self.task.connect(definitions, name="General")
        execution_queues = json.loads(self.task.get_configuration_object(name="Queues") or "")

        self.execution_queues = []
        buckets = defaultdict(int)
        for queue in execution_queues:
            if not queue.get("id"):
                queue["id"] = self._get_queue_id(queue.get("name"), raise_=True)
            queue["max_jobs"] = int(queue.get("max_jobs", 0))
            name = queue.get("name") or queue.get("id")
            if not self.queue_statistics.get(queue.get("id")):
                self.queue_statistics[queue.get("id")] = self.QueueStat()
            self.queue_statistics[queue.get("id")].update(dict(name=name, max_jobs=queue["max_jobs"], running=0))
            buckets[queue.get("id")] += 1
            self.execution_queues.append(
                self.QueueDefinition(
                    name=name,
                    id=queue.get("id"),
                    max_jobs=queue["max_jobs"],
                )
            )

        # Remove duplicate queues (possibly with different max_jobs, last one wins)
        for queue in self.execution_queues[:]:
            if buckets[queue.id] > 1:
                self.execution_queues.remove(queue)
                buckets[queue.id] -= 1

        self.monitored_queue_name = definitions.get("monitor_queue_name")
        self.monitored_queue_id = definitions.get("monitor_queue_id") or self._get_queue_id(
            self.monitored_queue_name, raise_=True
        )

        if not self.k8s_pending_queue_id:
            self.k8s_pending_queue_id = self._get_queue_id(self.k8s_pending_queue)

        self.task.get_logger().report_table(
            title="monitored",
            series="queue",
            iteration=0,
            table_plot=[
                ["monitored queue", "monitored queue id"],
                [self.monitored_queue_name, self.monitored_queue_id],
            ],
        )
        table = [["name", "queue id", "job limit"]]
        for q in self.execution_queues:
            table += [[q.name, q.id, q.max_jobs]]
        self.task.get_logger().report_table(title="execution", series="queue", iteration=0, table_plot=table)

        self.max_running_jobs = sum(q.max_jobs for q in self.execution_queues)

    def _find_running_pending_tasks(self):
        queues = list(self.queue_statistics.keys()) + [self.k8s_pending_queue_id]
        running_tasks = self.client.session.send_request(
            service="tasks",
            action="get_all",
            json={
                "status": ["in_progress", "queued"],
                "only_fields": ["id", "started", "status_changed", "runtime", "status", "execution.queue"],
                "_any_": {"pattern": "|".join(("({})".format(q) for q in queues)), "fields": ["execution.queue"]},
            },
        )
        if not running_tasks.ok:
            self.logger.error("Failed querying running tasks: {}".format(running_tasks))
            return None
        running_tasks = tasks_service.GetAllResponse(**running_tasks.json()["data"]).tasks
        return running_tasks

    def _loop_step(self):
        c = self.client
        qstats = self.queue_statistics

        # Check if we have any jobs waiting in the monitored queue
        monitored_res = c.queues.get_by_id(queue=self.monitored_queue_id)
        if not monitored_res:
            self.logger.warning("Monitored Queue [{}] was not found".format(self.monitored_queue_name))
            return

        # Check if we have available slots (i.e. at least one execution queue is empty)

        running_tasks = self._find_running_pending_tasks()

        # Update statistics
        self.current_running_jobs = len(running_tasks)
        for q_id, q in qstats.items():
            qstats[q_id]["running"] = len([t for t in running_tasks if (t.runtime or {}).get("_execution_q") == q_id])

        if not monitored_res.entries:
            self.logger.debug("Monitored Queue [{}] has no pending jobs, sleeping".format(self.monitored_queue_name))
            return

        # Find a free execution queue
        next_free_queue = next((queue_id for queue_id, queue in qstats.items() if queue.running < queue.max_jobs), None)

        # Tasks are already waiting in all queue, nothing for us to do.
        if not next_free_queue:
            self.logger.info(
                "{} jobs waiting in queue {} but all Execution Queues are full, sleeping:\n{}".format(
                    len(monitored_res.entries),
                    self.monitored_queue_name,
                    "\n".join((" - {}: {}/{} jobs".format(q.name, q.running, q.max_jobs) for q in qstats.values())),
                )
            )
            return

        if self._schedule_schedule_from_queue(
            self.monitored_queue_id, self.monitored_queue_name, next_free_queue, qstats[next_free_queue].name
        ):
            self.current_running_jobs += 1
            qstats[next_free_queue]["running"] += 1
        else:
            self.logger.warning("Failed scheduling task to queue, sleeping")

    def _schedule_schedule_from_queue(self, q_id, q_name, execution_q_id, execution_q_name):
        result = self.client.queues.get_next_task(queue=q_id)
        if not result or not result.entry:
            raise ValueError("obtaining task from queue {} [{}]".format(q_name, q_id))
        task_id = result.entry.task
        # mark with runtime properties
        tasks = self.client.tasks.get_all(id=[task_id], only_fields=["id", "runtime"])
        if not tasks:
            raise ValueError("locating task {} pulled from {}".format(task_id, q_name))
        runtime = dict(**(tasks[0].runtime or {}))
        runtime["_execution_q"] = execution_q_id
        runtime["_scheduled_q"] = q_id
        self.client.tasks.stop(task=task_id, force=True)
        self.client.tasks.edit(task=task_id, runtime=runtime, force=True)
        self.client.tasks.enqueue(task=task_id, queue=execution_q_id, status_reason="spillover controller schedule")
        self.logger.info("Scheduled task [{}] on queue {} [{}]".format(task_id, execution_q_name, execution_q_id))
        return True

    def daemon(self, task_cb=None):
        self.task = Task.init(project_name="DevOps", task_name="spillover controller", task_type="application")

        if task_cb:
            task_cb(self.task)

        self.client = APIClient()

        print("Starting task monitoring loop")

        first = True
        while True:
            if not first:
                sleep(5.0)
            first = False

            # noinspection PyBroadException
            try:
                self.refresh_configuration()
            except Exception as ex:
                self.logger.warning("Failed parsing new configuration object: {}".format(ex))

            # noinspection PyBroadException
            try:
                self._loop_step()
            except Exception as ex:
                self.logger.warning(
                    "Something happened during queue processing, " "we will try in a few seconds: {}".format(ex)
                )
                continue

            self.update_statistics()


if __name__ == "__main__":
    controller = SpilloverController()

    # controller.initial_definitions.monitor_queue_name = "GULLY"
    #
    # def callback(task):
    #     task.set_configuration_object(
    #         "Queues",
    #         json.dumps([
    #             {"name": "on-prem-queue", "max_jobs": 1},
    #             {"name": "on-prem-queue", "max_jobs": 2},
    #             {"name": "cloud-queue", "max_jobs": 1},
    #         ])
    #     )
    #
    # controller.daemon(callback)
    controller.daemon()

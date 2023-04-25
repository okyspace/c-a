from threading import Lock

from plots import (
    get_plotly_for_resource, get_plotly_number_idle_workers,
    get_running_instances_plot
)


from clearml import Task
from clearml.automation.auto_scaler import AutoScaler as BasicScaler
from clearml.backend_api.session.client import APIError


class CrashDB:
    def __init__(self):
        self.lock = Lock()
        self.crashed = {}  # worker_id -> task_id

    def add(self, worker_id, task_id):
        with self.lock:
            self.crashed[worker_id] = task_id

    def pop_all(self):
        with self.lock:
            crashed = self.crashed
            self.crashed = {}
        return list(crashed.items())


class AutoScaler(BasicScaler):
    def __init__(self, config, driver, logger=None):
        super().__init__(config, driver, logger)
        self.crashed_tasks = CrashDB()

    def worker_crashed(self, worker_id):
        """Callback from sub classes to notify a worker has crashed"""
        worker = self._find_worker(worker_id)
        if not worker:
            self.logger.warning('Unknown worker %r crashed', worker_id)
            return

        worker_task = worker.task
        if not worker_task:
            self.logger.info('Worker %r does not have an active task', worker_id)
            return

        # worker.task don't have status
        task = self.api_client.tasks.get_by_id(worker_task.id)
        # We prefer to be safe and only restore tasks that are in progress. We
        # might miss some that crashed, but it's better than reviving a task
        # the user aborted.
        if task.status != Task.TaskStatusEnum.in_progress:
            self.logger.info('Crashed worker %r has not associated task in running state', worker_id)
            return

        self.crashed_tasks.add(worker_id, task.id)
        try:
            # Notify backend the worker died
            self.logger.info('Unregistering worker %r', worker_id)
            self.api_client.workers.unregister(worker_id)
        except APIError:
            self.logger.exception('Cannot unregister worker %r', worker_id)

    def _find_worker(self, worker_id):
        for worker in self.api_client.workers.get_all():
            if worker.id == worker_id:
                return worker

    def extra_allocations(self):
        """Hook for subclass to use"""
        return self.crashed_tasks.pop_all()

    def gen_worker_prefix(self, resource, resource_conf):
        instance_type = resource_conf[self.driver.instance_type_key()]

        return '{workers_prefix}:{worker_type}:{instance_type}'.format(
            workers_prefix=self.workers_prefix,
            worker_type=resource,
            instance_type=instance_type,
        )

    def report_app_stats(self, logger, queue_id_to_name, up_machines, idle_workers):
        self.report_resource_and_queues(
            logger, self.resource_to_queue, queue_id_to_name)
        self.report_instances(logger, idle_workers)
        self.report_usage(logger, up_machines)

    def report_resource_and_queues(self, logger, resource_to_queue, queue_id_to_name):
        q_names = set(resource_to_queue.values())
        q_name_to_id = {q_name: q_id for q_id, q_name in queue_id_to_name.items() if q_name in q_names}
        resource_to_queue_names = {
            queue_id_to_name.get(queue_name, queue_name):
                len(self.api_client.queues.get_by_id(q_name_to_id[queue_name]).entries)
            for resource, queue_name in resource_to_queue.items()
        }
        logger.report_plotly(
            title="Resources and queues",
            series="",
            figure=get_plotly_for_resource(resource_to_queue_names)
        )

    def report_instances(self, logger, idle_workers):
        # Workers running in the app
        logger.report_plotly(
            title="Idle Instances",
            series="",
            figure=get_plotly_number_idle_workers(idle_workers=idle_workers)
        )

    def report_usage(self, logger, up_machines):
        queue_resources = []
        for queue in self.queues.values():
            queue_resources.extend(queue)
        fig = get_running_instances_plot(
             queue_resources=queue_resources,
             up_machines=up_machines
        )
        logger.report_plotly(
             title="Instances", series="",
             figure=fig,
        )

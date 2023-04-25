from threading import Lock, Thread
from time import sleep

import boto3
from botocore.exceptions import ClientError

from clearml.automation.aws_driver import AWSDriver as BasicDriver

spot_check_interval = 60
ok_statuses = {'pending', 'running'}


class Instances:
    """A thread safe instance store"""
    def __init__(self):
        self.instances = {}  # id -> instance
        self.lock = Lock()

    def __iter__(self):
        with self.lock:
            return iter(list(self.instances.items()))

    def add(self, instance_id, worker_id):
        with self.lock:
            self.instances[instance_id] = worker_id

    def remove(self, instance_id):
        with self.lock:
            del self.instances[instance_id]


class AWSDriver(BasicDriver):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.spot_instances = Instances()
        self.scaler = None
        self._monitor_thread = Thread(target=self.monitor_spots)
        self._monitor_thread.daemon = True

    def spin_up_worker(self, resource_conf, worker_prefix, queue_name, task_id=None):
        if not self._monitor_thread.is_alive():
            self._monitor_thread.start()
        instance_id = super().spin_up_worker(resource_conf, worker_prefix, queue_name, task_id)
        if not resource_conf['is_spot']:
            return instance_id

        worker_id = f'{worker_prefix}:{instance_id}'
        self.spot_instances.add(instance_id, worker_id)
        return instance_id

    def monitor_spots(self):
        while True:
            try:
                self._monitor_spots()
            except Exception as err:
                self.logger.exception('error in monitor: %r', err)

    def _monitor_spots(self):
        self.logger.info('monitor spots started')
        ec2 = boto3.resource("ec2", **self.creds())

        while True:
            sleep(spot_check_interval)
            self.logger.debug('monitor spots awake')
            for instance_id, worker_id in self.spot_instances:
                try:

                    instance = ec2.Instance(instance_id)
                    # instance.state is a function call to AWS
                    state = instance.state['Name']
                except ClientError:
                    self.logger.exception(
                        "Can not get state of instance %r, assuming terminated "
                        "(you can verify this on your AWS console)", instance_id)
                    state = 'terminated'

                self.logger.info('monitor_spots: %s - %s', instance_id, state)
                if state in ok_statuses:
                    continue

                self.logger.warning('instance %r crashed (worker %r)', instance_id, worker_id)
                self.spot_instances.remove(instance_id)
                self.scaler.worker_crashed(worker_id)

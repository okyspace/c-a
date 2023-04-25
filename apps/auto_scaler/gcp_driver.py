import json
from datetime import datetime, timedelta
from time import sleep
from uuid import uuid4

import attr
from clearml import Task
from clearml.automation.cloud_driver import CloudDriver

try:
    # noinspection PyPackageRequirements
    import googleapiclient.discovery
    from google.oauth2.service_account import Credentials

    Task.add_requirements("google-api-python-client")
except ImportError as err:
    raise ImportError(
        "GCPAutoScaler requires 'google-api-python-client' package, it was not found\n"
        "install with: pip install google-api-python-client"
    ) from err


class GCPError(Exception):
    pass


@attr.s
class GCPDriver(CloudDriver):
    gcp_credentials = attr.ib(default="")
    gcp_project_id = attr.ib(default="")
    gcp_zone = attr.ib(default="")

    @classmethod
    def from_config(cls, config):
        obj = super().from_config(config)
        hyper_params = config['hyper_params']
        return attr.evolve(
            obj,
            gcp_credentials=hyper_params.get('gcp_credentials'),
            gcp_project_id=hyper_params['gcp_project_id'],
            gcp_zone=hyper_params['gcp_zone'],
        )

    def __attrs_post_init__(self):
        creds = self._load_credentials()
        if creds:
            self.client = googleapiclient.discovery.build('compute', 'v1', credentials=creds)

    def _load_credentials(self):
        if not self.gcp_credentials:
            return None

        creds = json.loads(self.gcp_credentials)
        return Credentials.from_service_account_info(creds)

    def instance_id_command(self):
        return 'curl -H "Metadata-Flavor: Google" http://metadata/computeMetadata/v1/instance/id'

    def kind(self):
        return 'GCP'

    def instance_type_key(self):
        return 'machine_type'

    def spin_up_worker(self, resource_conf, worker_prefix, queue_name, task_id=None):
        startup_script = self.gen_user_data(worker_prefix, queue_name, task_id)

        launch_spec = create_launch_spec(self, resource_conf, startup_script)
        response = self.client.instances().insert(
            project=self.gcp_project_id,
            zone=self.gcp_zone,
            body=launch_spec).execute()
        self.logger.info('Response: %s', response)

        self.wait_for_operation(response['name'], timedelta(minutes=10))
        return response['targetId']

    def spin_down_worker(self, instance_id):
        resp = self.client.instances().delete(
            project=self.gcp_project_id,
            zone=self.gcp_zone,
            instance=instance_id).execute()
        self.logger.info('spin_down_worker %s: %s', instance_id, resp)

    def wait_for_operation(self, name, timeout):
        start = datetime.now()
        while datetime.now() - start <= timeout:
            resp = self.client.zoneOperations().get(
                project=self.gcp_project_id,
                zone=self.gcp_zone,
                operation=name).execute()

            if resp['status'] == 'DONE':
                if 'error' in resp:
                    self.logger.error(str(resp['error']))
                    raise GCPError(resp['error'])
                # No error - return
                return

            sleep(5)

        raise GCPError(f'operation {name!r} did not terminate after {timeout}')


def zone2region(zone):
    """
    >>> zone2region('us-central1-a')
    'us-central1'
    """
    i = zone.rfind('-')
    if i == -1:
        raise ValueError(f'no "-" in {zone!r}')
    return zone[:i]


def create_launch_spec(settings, resource_conf, startup_script):
    project_id = settings.gcp_project_id
    zone_short = settings.gcp_zone
    region = zone2region(zone_short)
    zone = f'projects/{project_id}/zones/{zone_short}'
    machine_type = resource_conf['machine_type']
    # Must match r'[a-z]([-a-z0-9]*[a-z0-9])?'
    device_name = f'clearml-worker-{uuid4().hex}'

    launch_spec = {
      'name': device_name,
      'zone': zone,
      'machineType': f'{zone}/machineTypes/{machine_type}',
      'displayDevice': {
        'enableDisplay': False,
      },
      'metadata': {
        'items': [
          {
            'key': 'startup-script',
            'value': startup_script,
          }
        ]
      },
      'tags': {
        'items': []
      },
      'disks': [
        {
          'type': 'PERSISTENT',
          'boot': True,
          'mode': 'READ_WRITE',
          'autoDelete': True,
          'deviceName': device_name,
          'initializeParams': {
            'sourceImage': resource_conf['source_image'],
            'diskType': f'{zone}/diskTypes/pd-balanced',
            'diskSizeGb': '10',
            'labels': {}
          },
          'diskEncryptionKey': {}
        }
      ],
      'canIpForward': False,
      'networkInterfaces': [
        {
          'subnetwork': f'projects/{project_id}/regions/{region}/subnetworks/default',
          'accessConfigs': [
            {
              'name': 'External NAT',
              'type': 'ONE_TO_ONE_NAT',
              'networkTier': 'PREMIUM'
            }
          ],
          'aliasIpRanges': []
        }
      ],
      'description': '',
      'labels': {},
      'scheduling': {
        'preemptible': False,
        'automaticRestart': True,
        'nodeAffinities': []
      },
      'deletionProtection': False,
      'reservationAffinity': {
        'consumeReservationType': 'ANY_RESERVATION'
      },
      'serviceAccounts': [
        {
          'email': 'default',
          'scopes': [
            'https://www.googleapis.com/auth/devstorage.read_only',
            'https://www.googleapis.com/auth/logging.write',
            'https://www.googleapis.com/auth/monitoring.write',
            'https://www.googleapis.com/auth/servicecontrol',
            'https://www.googleapis.com/auth/service.management.readonly',
            'https://www.googleapis.com/auth/trace.append'
          ]
        }
      ],
      'shieldedInstanceConfig': {
        'enableSecureBoot': False,
        'enableVtpm': True,
        'enableIntegrityMonitoring': True
      },
      'confidentialInstanceConfig': {
        'enableConfidentialCompute': False
      }
    }

    gpu_count = resource_conf.get('gpu_count', 0)
    if gpu_count > 0:
        gpu_type = resource_conf['gpu_type']
        launch_spec['guestAccelerators'] = {
            'acceleratorCount': 1,
            'acceleratorType': f'{zone}/acceleratorTypes/{gpu_type}',
        }

    # TODO: Need testing
    if resource_conf.get('preemptible'):
        launch_spec['scheduling']['preemptible'] = True
        launch_spec['scheduling']['onHostMaintenance'] = 'TERMINATE',

    return launch_spec

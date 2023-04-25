from time import sleep

from clearml import Task


def daemon():
    task = Task.init(project_name='DevOps', task_name='trigger manager', task_type='application')

    print('Starting trigger loop')

    first = True
    while True:
        if not first:
            sleep(60.0)
        first = False


if __name__ == "__main__":
    daemon()
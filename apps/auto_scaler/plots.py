import plotly.graph_objects as go
import plotly.express as px
import pandas as pd


def get_running_instances_plot(queue_resources, up_machines):
    fig = go.Figure()
    row = 0
    column = 0
    # Two objects max in each row
    for ec2_type, amount in queue_resources:
        value = up_machines.get(ec2_type, 0)
        fig.add_trace(go.Indicator(
            value=value if value >= 0 else 0,
            gauge={'axis': {'range': [None, amount]}},
            delta={'reference': 0},
            mode="gauge+number",
            title={'text': "%s Running Instances" % ec2_type},
            domain={'row': row, 'column': column}
            )
        )
        column += 1
        if column % 2 == 0:
            column = 0
            row += 1

    return fig.update_layout(
        grid={'rows': row+1, 'columns': 2, 'pattern': "independent"},
    )


def get_plotly_number_idle_workers(idle_workers):
    return go.Figure(go.Indicator(
        mode="number",
        value=len(idle_workers) if len(idle_workers) >= 0 else 0,
        domain={'x': [0, 1], 'y': [0, 1]},
        title={'text': "Idle instances"}))


def get_plotly_for_resource(resource_to_queue):
    resources_data = {
        'Queues': list(resource_to_queue.keys()),
        '# of Tasks': list(resource_to_queue.values())
    }
    data = pd.DataFrame(resources_data)
    return px.bar(data, x='Queues', y='# of Tasks', barmode='stack')

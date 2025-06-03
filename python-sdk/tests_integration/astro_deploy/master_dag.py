import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, List

from airflow import DAG, settings
from airflow.models import Connection, DagRun
from airflow.models.baseoperator import chain
from airflow.operators.bash import BashOperator
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import PythonOperator, get_current_context
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.slack.operators.slack_webhook import SlackWebhookOperator
from airflow.utils.session import create_session

if TYPE_CHECKING:
    from airflow.models.taskinstance import TaskInstance


SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#provider-alert")
SLACK_WEBHOOK_CONN = os.getenv("SLACK_WEBHOOK_CONN", "http_slack")
SLACK_USERNAME = os.getenv("SLACK_USERNAME", "airflow_app")
AWS_S3_CREDS = {
    "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID", "not_set"),
    "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY", "not_set"),
    "region_name": "us-east-1",
}
AMI_ID = os.getenv("AWS_AMI_ID", "not_set")
INBOUND_SECURITY_GROUP_ID = os.getenv("AWS_INBOUND_SECURITY_GROUP_ID", "not_set")
EC2_INSTANCE_ID_KEY = "ec2_instance_id"
INSTANCE_PUBLIC_IP = "instance_public_ip"
SFTP_USERNAME = os.getenv("SFTP_USERNAME", "not_set")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", "not_set")
FTP_USERNAME = os.getenv("FTP_USERNAME", "not_set")
FTP_PASSWORD = os.getenv("FTP_PASSWORD", "not_set")


def get_report(dag_run_ids: List[str], **context: Any) -> None:  # noqa: C901
    """Fetch dags run details and generate report"""

    with create_session() as session:
        last_dags_runs: List[DagRun] = session.query(DagRun).filter(DagRun.run_id.in_(dag_run_ids)).all()
        message_list: List[str] = []

        airflow_version = context["ti"].xcom_pull(task_ids="get_airflow_version")
        airflow_executor = context["ti"].xcom_pull(task_ids="get_airflow_executor")
        astro_sdk_version = context["ti"].xcom_pull(task_ids="get_astro_sdk_version")
        astro_cloud_provider = context["ti"].xcom_pull(task_ids="get_astro_cloud_provider")

        report_details = [
            f"*{header}:* `{value}`\n"
            for header, value in [
                ("Runtime version", os.getenv("ASTRONOMER_RUNTIME_VERSION", "N/A")),
                ("Python version", os.getenv("PYTHON_VERSION", "N/A")),
                ("Airflow version", airflow_version),
                ("Executor", airflow_executor),
                ("Astro-SDK version", astro_sdk_version),
                ("Cloud provider", astro_cloud_provider),
            ]
        ]

        report_details.insert(0, "Results generated for:\n\n")
        report_details.append("\n")  # Adding an additional newline at the end
        webserver_base_url = os.environ["AIRFLOW__WEBSERVER__BASE_URL"]
        master_dag_deployment_link = (
            f"{webserver_base_url}/dags/example_master_dag/grid?search=example_master_dag"
        )
        deployment_message = (
            f"\n <{master_dag_deployment_link}|Link> to the master DAG deployment of the above run \n"
        )

        dag_count, failed_dag_count = 0, 0
        for dr in last_dags_runs:
            dr_status = f" *{dr.dag_id} : {dr.get_state()}* \n"
            dag_count += 1
            failed_tasks = []
            for ti in dr.get_task_instances():
                task_code = ":black_circle: "
                if not ((ti.task_id == "end") or (ti.task_id == "get_report")):
                    if ti.state == "success":
                        continue
                    elif ti.state == "failed":
                        task_code = ":red_circle: "
                        failed_tasks.append(f"{task_code} {ti.task_id} : {ti.state} \n")
                    elif ti.state == "upstream_failed":
                        task_code = ":large_orange_circle: "
                        failed_tasks.append(f"{task_code} {ti.task_id} : {ti.state} \n")
                    else:
                        failed_tasks.append(f"{task_code} {ti.task_id} : {ti.state} \n")
            if failed_tasks:
                message_list.append(dr_status)
                message_list.extend(failed_tasks)
                failed_dag_count += 1

        output_list = [
            f"*Total DAGS*: {dag_count} \n",
            f"*Success DAGS*: {dag_count-failed_dag_count} :green_apple: \n",
            f"*Failed DAGS*: {failed_dag_count} :apple: \n \n",
        ]
        output_list = report_details + output_list
        if failed_dag_count > 0:
            output_list.append("*Failure Details:* \n")
            output_list.extend(message_list)
        dag_run = context["dag_run"]
        task_instances = dag_run.get_task_instances()

        task_failure_message_list: List[str] = [
            f":red_circle: {ti.task_id} \n" for ti in task_instances if ti.state == "failed"
        ]

        if task_failure_message_list:
            output_list.append(
                "\nSome of Master DAG tasks failed, please check with deployment link below \n"
            )
            output_list.extend(task_failure_message_list)
        output_list.append(deployment_message)
        logging.info("%s", "".join(output_list))
        # Send dag run report on Slack
        try:
            SlackWebhookOperator(
                task_id="slack_alert",
                slack_webhook_conn_id=SLACK_WEBHOOK_CONN,
                message="".join(output_list),
                channel=SLACK_CHANNEL,
                username=SLACK_USERNAME,
            ).execute(context=None)
        except Exception as exception:
            logging.exception("Error occur while sending slack alert.")
            raise exception


def prepare_dag_dependency(task_info, execution_time):
    """Prepare list of TriggerDagRunOperator task and dags run ids for dags of same providers"""
    _dag_run_ids = []
    _task_list = []
    for _example_dag in task_info:
        _task_id = list(_example_dag.keys())[0]

        _run_id = f"{_task_id}_{_example_dag.get(_task_id)}_" + execution_time
        _dag_run_ids.append(_run_id)
        _task_list.append(
            TriggerDagRunOperator(
                task_id=_task_id,
                trigger_dag_id=_example_dag.get(_task_id),
                trigger_run_id=_run_id,
                wait_for_completion=True,
                reset_dag_run=True,
                execution_date=execution_time,
                allowed_states=["success", "failed"],
            )
        )
    return _task_list, _dag_run_ids


def start_sftp_ftp_services_method():
    import boto3

    ec2 = boto3.resource("ec2", **AWS_S3_CREDS)
    instance = ec2.create_instances(
        ImageId=AMI_ID,
        MinCount=1,
        MaxCount=1,
        InstanceType="t2.micro",
        SecurityGroupIds=[INBOUND_SECURITY_GROUP_ID],
    )
    print("instance : ", instance)
    instance_id = instance[0].instance_id
    ti = get_current_context()["ti"]
    ti.xcom_push(key=EC2_INSTANCE_ID_KEY, value=instance_id)
    time.sleep(
        120
    )  # Need to wait for ecs instance to be up otherwise the `boto3's describe_instances` call fails.
    while get_instances_status(instance_id) != "running":
        logging.info("Waiting for Instance to be available in running state. Sleeping for 30 seconds.")
        time.sleep(30)


def get_instances_status(instance_id: str) -> str:
    """Get the instance status by id"""
    import boto3

    client = boto3.client("ec2", **AWS_S3_CREDS)
    response = client.describe_instances(
        InstanceIds=[instance_id],
    )
    print("response : ", response)
    instance_details = response["Reservations"][0]["Instances"][0]
    instance_state: str = instance_details["State"]["Name"]
    print("instance_state : ", instance_state)
    if instance_state == "running":
        ti = get_current_context()["ti"]
        ti.xcom_push(key=INSTANCE_PUBLIC_IP, value=instance_details["PublicIpAddress"])
    return instance_state


def create_sftp_ftp_airflow_connection(task_instance: Any) -> None:
    """
    Checks if airflow connection exists, if yes then deletes it.
    Then, create a new sftp_default, ftp_default connection.
    """
    sftp_conn = Connection(
        conn_id="sftp_conn",
        conn_type="sftp",
        host=task_instance.xcom_pull(key=INSTANCE_PUBLIC_IP, task_ids=["start_sftp_ftp_services"])[0],
        login=SFTP_USERNAME,
        password=SFTP_PASSWORD,
    )  # create a connection object

    ftp_conn = Connection(
        conn_id="ftp_conn",
        conn_type="ftp",
        host=task_instance.xcom_pull(key=INSTANCE_PUBLIC_IP, task_ids=["start_sftp_ftp_services"])[0],
        login=FTP_USERNAME,
        password=FTP_PASSWORD,
    )  # create a connection object

    session = settings.Session()
    for conn in [sftp_conn, ftp_conn]:
        connection = session.query(Connection).filter_by(conn_id=conn.conn_id).one_or_none()
        if connection is None:
            logging.info("Connection %s doesn't exist.", str(conn.conn_id))
        else:
            session.delete(connection)
            session.commit()
            logging.info("Connection %s deleted.", str(conn.conn_id))

        session.add(conn)
        session.commit()  # it will insert the connection object programmatically.
        logging.info("Connection %s is created", conn.conn_id)


def terminate_instance(task_instance: "TaskInstance") -> None:  # noqa: F821
    """Terminate ec2 instance by instance id"""
    import boto3

    ec2 = boto3.client("ec2", **AWS_S3_CREDS)
    ec2_instance_id_xcom = task_instance.xcom_pull(
        key=EC2_INSTANCE_ID_KEY, task_ids=["start_sftp_ftp_services"]
    )[0]
    ec2.terminate_instances(
        InstanceIds=[
            ec2_instance_id_xcom,
        ],
    )


with DAG(
    dag_id="example_master_dag",
    schedule=None,
    start_date=datetime(2023, 1, 1),
    catchup=False,
    tags=["master_dag"],
) as dag:
    start = PythonOperator(
        task_id="start",
        python_callable=lambda: time.sleep(30),
    )

    list_installed_pip_packages = BashOperator(
        task_id="list_installed_pip_packages", bash_command="pip freeze"
    )

    get_airflow_version = BashOperator(
        task_id="get_airflow_version", bash_command="airflow version", do_xcom_push=True
    )

    get_airflow_executor = BashOperator(
        task_id="get_airflow_executor",
        bash_command="airflow config get-value core executor",
        do_xcom_push=True,
    )

    get_astro_sdk_version = BashOperator(
        task_id="get_astro_sdk_version",
        bash_command="pip show astro-sdk-python | grep -i version | awk '{print $2}'",
        do_xcom_push=True,
    )

    get_astro_cloud_provider = BashOperator(
        task_id="get_astro_cloud_provider",
        bash_command=(
            "[[ $AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID == *azure* ]] && echo 'azure' ||"
            "([[ $AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID == *s3* ]] && echo 'aws' ||"
            "([[ $AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID == *gcs* ]] && echo 'gcs' ||"
            "echo 'unknown'))"
        ),
        do_xcom_push=True,
    )

    start_sftp_ftp_services = PythonOperator(
        task_id="start_sftp_ftp_services",
        python_callable=start_sftp_ftp_services_method,
    )

    create_sftp_ftp_default_airflow_connection = PythonOperator(
        task_id="create_sftp_ftp_default_airflow_connection",
        python_callable=create_sftp_ftp_airflow_connection,
    )

    terminate_ec2_instance = PythonOperator(
        task_id="terminate_instance", trigger_rule="all_done", python_callable=terminate_instance
    )

    dag_run_ids = []

    load_file_task_info = [
        {"example_google_bigquery_gcs_load_and_save": "example_google_bigquery_gcs_load_and_save"},
        {"example_amazon_s3_postgres_load_and_save": "example_amazon_s3_postgres_load_and_save"},
        {"example_amazon_s3_postgres": "example_amazon_s3_postgres"},
        {"example_load_file": "example_load_file"},
    ]

    load_file_trigger_tasks, ids = prepare_dag_dependency(load_file_task_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*load_file_trigger_tasks)

    transform_task_info = [
        {"example_amazon_s3_snowflake_transform": "example_amazon_s3_snowflake_transform"},
        {"example_transform_mssql": "example_transform_mssql"},
    ]

    transform_trigger_tasks, ids = prepare_dag_dependency(transform_task_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*transform_trigger_tasks)

    dataframe_task_info = [
        {"example_dataframe": "example_dataframe"},
    ]

    dataframe_trigger_tasks, ids = prepare_dag_dependency(dataframe_task_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*dataframe_trigger_tasks)

    append_task_info = [
        {"example_append": "example_append"},
        {"example_snowflake_partial_table_with_append": "example_snowflake_partial_table_with_append"},
    ]

    append_trigger_tasks, ids = prepare_dag_dependency(append_task_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*append_trigger_tasks)

    merge_trigger_tasks = [{"example_merge_bigquery": "example_merge_bigquery"}]

    merge_trigger_tasks, ids = prepare_dag_dependency(merge_trigger_tasks, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*merge_trigger_tasks)

    dynamic_task_info = [
        {"example_dynamic_map_task": "example_dynamic_map_task"},
        {"example_dynamic_task_template": "example_dynamic_task_template"},
    ]

    dynamic_task_trigger_tasks, ids = prepare_dag_dependency(dynamic_task_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*dynamic_task_trigger_tasks)

    data_validation_dags_info = [
        {"data_validation_check_column": "data_validation_check_column"},
    ]

    data_validation_trigger_tasks, ids = prepare_dag_dependency(data_validation_dags_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*data_validation_trigger_tasks)

    dataset_dags_info = [
        {"example_dataset_producer": "example_dataset_producer"},
    ]

    dataset_trigger_tasks, ids = prepare_dag_dependency(dataset_dags_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*dataset_trigger_tasks)

    cleanup_snowflake_task_info = [{"example_snowflake_cleanup": "example_snowflake_cleanup"}]

    cleanup_snowflake_trigger_tasks, ids = prepare_dag_dependency(cleanup_snowflake_task_info, "{{ ds }}")
    dag_run_ids.extend(ids)
    chain(*cleanup_snowflake_trigger_tasks)

    report = PythonOperator(
        task_id="get_report",
        python_callable=get_report,
        op_kwargs={"dag_run_ids": dag_run_ids},
        trigger_rule="all_done",
        provide_context=True,
    )

    end = DummyOperator(
        task_id="end",
        trigger_rule="all_success",
    )

    (  # skipcq PYL-W0104
        start
        >> start_sftp_ftp_services
        >> create_sftp_ftp_default_airflow_connection
        >> [  # skipcq PYL-W0104
            list_installed_pip_packages,
            get_airflow_version,
            get_airflow_executor,
            get_astro_sdk_version,
            get_astro_cloud_provider,
            load_file_trigger_tasks[0],
            transform_trigger_tasks[0],
            dataframe_trigger_tasks[0],
            append_trigger_tasks[0],
            merge_trigger_tasks[0],
            dynamic_task_trigger_tasks[0],
            data_validation_trigger_tasks[0],
            dataset_trigger_tasks[0],
            cleanup_snowflake_trigger_tasks[0],
        ]
    )

    last_task = [
        list_installed_pip_packages,
        get_airflow_version,
        get_airflow_executor,
        get_astro_sdk_version,
        get_astro_cloud_provider,
        load_file_trigger_tasks[-1],
        transform_trigger_tasks[-1],
        dataframe_trigger_tasks[-1],
        append_trigger_tasks[-1],
        merge_trigger_tasks[-1],
        dynamic_task_trigger_tasks[-1],
        data_validation_trigger_tasks[-1],
        dataset_trigger_tasks[-1],
        cleanup_snowflake_trigger_tasks[-1],
    ]

    last_task >> end  # skipcq PYL-W0104
    last_task >> report  # skipcq PYL-W0104
    last_task >> terminate_ec2_instance  # skipcq PYL-W0104

import argparse
import logging
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psutil

# Configure the logger
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def update_database(
    db_name,
    main_path,
    config_path,
    server_user,
    hard_update,
    use_venv,
):
    log_file = f"/tmp/upd_odoo_log_{db_name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    if use_venv:
        command = [
            "sudo",
            "-u",
            server_user,
            "bash",
            "-c",
            f"source '{main_path}/venv/bin/activate' && click-odoo-update -c '{config_path}' -d '{db_name}' --logfile '{log_file}' --watcher-max-seconds 0 --log-level error",
        ]
    else:
        command = [
            "sudo",
            "-u",
            server_user,
            "bash",
            "-c",
            f"click-odoo-update -c '{config_path}' -d '{db_name}' --logfile '{log_file}' --watcher-max-seconds 0 --log-level error",
        ]

    if hard_update:
        command[-1] += " --update-all"

    error_found = False

    logger.info(f"Starting database update for {db_name}")
    try:
        result = subprocess.run(command, check=True)
        logger.info(
            f"Command executed for {db_name} with return code {result.returncode}"
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to update database {db_name}. Error: {e}")
        error_found = True

    # Always check logs for errors, regardless of the process exit code
    with open(log_file, "r") as file:
        log_content = file.read()

        if re.search(r"ERROR|Traceback", log_content, re.IGNORECASE):
            logger.error(
                f"Errors found during the update of database {db_name}. Check the logs for more information."
            )
            logger.error(f"Logs for {db_name}:\n{log_content}")
            error_found = True

    return 1 if error_found else 0


def monitor_and_update(
    databases,
    main_path,
    config_path,
    server_user,
    max_cpu_usage=90,
    max_ram_usage=80,
    hard_update=False,
    use_venv=True,
):
    active_tasks = 0
    error_encountered = False

    def task_done(future, db):
        nonlocal active_tasks
        try:
            result = future.result()
            if result != 0:  # If the result is not 0, set error_encountered to True
                error_encountered = True
                logger.error(f"Database update for {db} failed.")
            else:
                logger.info(f"Database update for {db} completed successfully.")
        except Exception as exc:
            error_encountered = True
            logger.error(f"Database update for {db} failed: {exc}")
        finally:
            active_tasks -= 1
            logger.info(f"Task completed for {db}, active tasks: {active_tasks}")

    with ThreadPoolExecutor() as executor:
        for db in databases:
            while True:
                cpu_usage = psutil.cpu_percent(interval=1)
                ram_usage = psutil.virtual_memory().percent

                if cpu_usage < max_cpu_usage and ram_usage < max_ram_usage:
                    future = executor.submit(
                        update_database,
                        db,
                        main_path,
                        config_path,
                        server_user,
                        hard_update,
                        use_venv,
                    )
                    active_tasks += 1
                    logger.info(f"Started task for {db}, active tasks: {active_tasks}")

                    future.add_done_callback(lambda f, db=db: task_done(f, db))
                    time.sleep(1)
                    break
                else:
                    time.sleep(5)

    if error_encountered:
        logger.error("========= Some databases failed to update. =============")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parallel Odoo database update")
    parser.add_argument(
        "--databases", required=True, help="List of databases separated by spaces"
    )
    parser.add_argument(
        "--main_path",
        required=True,
        help="Path to the main Odoo directory and virtual environment",
    )
    parser.add_argument(
        "--config", required=True, help="Path to the Odoo configuration file"
    )
    parser.add_argument("--user", required=True, help="Server user to execute commands")
    parser.add_argument(
        "--hard", action="store_true", help="Enable full update (all modules)"
    )
    parser.add_argument(
        "--use-venv", action="store_true", help="Use virtual environment during update"
    )

    args = parser.parse_args()

    databases = args.databases.split()
    monitor_and_update(
        databases,
        args.main_path,
        args.config,
        args.user,
        hard_update=args.hard,
        use_venv=args.use_venv,
    )

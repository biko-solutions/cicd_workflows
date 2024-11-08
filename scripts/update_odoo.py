import argparse
import logging
import os
import re
import subprocess
import sys
import threading
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


def tail_log_file(log_file, stop_event, max_wait_time=30):
    """Function to tail a log file and print the output to the logger."""

    # waiting for log-file to be created
    wait_time = 0
    while not os.path.exists(log_file) and wait_time < max_wait_time:
        logger.debug(
            f"[DEBUG] File {log_file} does not exist yet, waiting for its creation..."
        )
        time.sleep(1)
        wait_time += 1

    if not os.path.exists(log_file):
        logger.error(
            f"[ERROR] Log file {log_file} was not created after waiting for {max_wait_time} seconds."
        )
        return

    logger.debug("[DEBUG] Log file found, starting tail -f")

    with subprocess.Popen(
        ["tail", "-f", log_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        index = 0
        logger.debug("[DEBUG] tail -f process started")

        while not stop_event.is_set():
            logger.debug(f"[DEBUG] Checking before reading line, iteration {index}")
            index += 1

            # Check the process state
            if proc.poll() is not None:
                logger.debug("[DEBUG] tail process has finished")
                break

            line = proc.stdout.readline()
            if line:
                logger.debug("[DEBUG] Line received from stdout stream")
                logger.info(f"[LOG OUTPUT] {line.strip()}")
            else:
                logger.debug("[DEBUG] Line is empty, possibly end of stream")
                break

        # Terminate the process and log the message
        proc.terminate()
        logger.debug("[DEBUG] tail process terminated manually")


def update_database(
    db_name,
    main_path,
    config_path,
    server_user,
    hard_update,
    use_venv,
    show_log,
):
    log_file = f"/tmp/upd_odoo_log_{db_name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    if use_venv:
        command = [
            "sudo",
            "-u",
            server_user,
            "bash",
            "-c",
            f"source '{main_path}/venv/bin/activate' && click-odoo-update -c '{config_path}' -d '{db_name}' --logfile '{log_file}' --watcher-max-seconds 0 --log-level info",
        ]
    else:
        command = [
            "sudo",
            "-u",
            server_user,
            "bash",
            "-c",
            f"source '/home/{server_user}/.bashrc' && click-odoo-update -c '{config_path}' -d '{db_name}' --logfile '{log_file}' --watcher-max-seconds 0 --log-level info",
        ]

    if hard_update:
        command[-1] += " --update-all"

    error_found = False

    logger.info(f"Starting database update for {db_name}")
    logger.info(f"Logfile {log_file}")

    # Флаг для остановки потока
    stop_event = threading.Event()

    # Запуск потока для параллельного чтения логов, если show_log == True
    if show_log:
        log_thread = threading.Thread(target=tail_log_file, args=(log_file, stop_event))
        log_thread.start()

    try:
        result = subprocess.run(command, check=True)
        logger.info(
            f"Command executed for {db_name} with return code {result.returncode}"
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to update database {db_name}. Error: {e}")
        error_found = True
    finally:
        # Установка флага для остановки потока и ожидание его завершения
        if show_log:
            stop_event.set()
            log_thread.join()

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
    show_log=False,
):
    active_tasks = 0
    error_encountered = False

    def task_done(future, db):
        nonlocal active_tasks
        nonlocal error_encountered
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
                        show_log,
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
    parser.add_argument("--show_log", action="store_true", help="Show update log")

    args = parser.parse_args()

    databases = args.databases.split()
    monitor_and_update(
        databases,
        args.main_path,
        args.config,
        args.user,
        hard_update=args.hard,
        use_venv=args.use_venv,
        show_log=args.show_log,
    )

import os
import re
import sys
import time
import torch
import logging
import signal
import threading
import subprocess
import json
from pathlib import Path
from itertools import cycle
from dotenv import load_dotenv
from multiprocessing import Process, set_start_method
from auth.generator import WalletGenerator
from tabulate import tabulate
import curses
from pynvml import *
 
from sd_mining_core.base import BaseConfig, ModelUpdater
from sd_mining_core.utils import (
    check_cuda, get_hardware_description,
    fetch_and_download_config_files, get_local_model_ids,
    post_request, log_response, submit_job_result,
    initialize_logging_and_args,
    load_default_model, reload_model,
)

# Marker to indicate the start of a new run
RUN_MARKER = "INFO - Starting new run"

class MinerConfig(BaseConfig):
    def __init__(self, config_file, cuda_device_id=0):
        super().__init__(config_file, cuda_device_id)
        if not self.skip_signature:
            self.wallet_generator = WalletGenerator(config_file, abi_file = os.path.join(os.path.dirname(__file__), 'auth', 'abi.json'))
        load_dotenv()  # Load the environment variables
        
        miner_ids = self._load_and_validate_miner_ids()
        self.miner_id = self._assign_miner_id(miner_ids, cuda_device_id)

    def _load_and_validate_miner_ids(self):
        miner_ids = [os.getenv(f'MINER_ID_{i}') for i in range(self.num_cuda_devices)]
        if not self.skip_signature:
            self.wallet_generator.validate_miner_keys(miner_ids)

        composite_miner_ids = []
        evm_address_pattern = re.compile(r"^(0x[a-fA-F0-9]{40})(-[a-zA-Z0-9_]+)?$")
        for i, miner_id in enumerate(miner_ids):
            if miner_id is None:
                print(f"ERROR: Miner ID for GPU {i} not found in .env. Exiting...")
                raise ValueError(f"Miner ID for GPU {i} not found in .env.")
            
            match = evm_address_pattern.match(miner_id)
            if match:
                evm_address = match.group(1)
                suffix = match.group(2)
                
                if suffix:
                    # Miner ID is a valid EVM address with a non-empty suffix
                    composite_miner_ids.append(miner_id)
                else:
                    # Miner ID is a valid EVM address without a suffix or with an empty suffix
                    # Get the GPU UUID using nvidia-smi
                    try:
                        output = subprocess.check_output(["nvidia-smi", "-L"]).decode("utf-8")
                        gpu_info = output.split("\n")[i].strip()
                        gpu_uuid_segment = gpu_info.split("GPU-")[1].split("-")[0]
                        short_uuid = gpu_uuid_segment[:6]
                        composite_miner_id = f"{evm_address}-{short_uuid}"
                        composite_miner_ids.append(composite_miner_id)
                    except (subprocess.CalledProcessError, IndexError):
                        # nvidia-smi command failed or UUID not found
                        print(f"WARNING: Failed to retrieve GPU UUID for GPU {i}. Using original miner ID.")
                        composite_miner_ids.append(miner_id)
            else:
                # Miner ID is not a valid EVM address
                print(f"WARNING: Miner ID {miner_id} for GPU {i} is not a valid EVM address.")
                composite_miner_ids.append(miner_id)
        
        return composite_miner_ids

    def _assign_miner_id(self, miner_ids, cuda_device_id):
        if self.num_cuda_devices > 1 and miner_ids[cuda_device_id]:
            return miner_ids[cuda_device_id]
        elif miner_ids[0]:
            return miner_ids[0]
        else:
            print("ERROR: miner_id not found in .env. Exiting...")
            raise ValueError("miner_id not found in .env.")

def load_config(filename='config.toml', cuda_device_id=0):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, filename)
    return MinerConfig(config_path, cuda_device_id)

def send_miner_request(config, model_id, min_deadline):
    request_data = {
        "miner_id": config.miner_id,
        "model_id": model_id,
        "min_deadline": min_deadline
    }
    if time.time() - config.last_heartbeat >= 60:
        request_data['hardware'] = get_hardware_description(config)
        request_data['version'] = config.version
        config.last_heartbeat = time.time()
        logging.debug(f"Heartbeat updated at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(config.last_heartbeat))} with hardware '{request_data['hardware']}' and version {config.version} for miner ID {config.miner_id}.")
    
    start_time = time.time()
    response = post_request(config, config.base_url + "/miner_request", request_data, config.miner_id)
    end_time = time.time()
    request_latency = end_time - start_time

    # Assuming response.text contains the full text response from the server
    warning_indicator = "Warning:"
    if response and warning_indicator in response.text:
        # Extract the warning message and use strip() to remove any trailing quotation marks
        warning_message = response.text.split(warning_indicator)[1].strip('"')
        print(f"WARNING: {warning_message}")

    response_data = log_response(response, config.miner_id)

    try:
        # Check if the response contains a valid job and print the friendly message
        if response_data and 'job_id' in response_data and 'model_id' in response_data:
            job_id = response_data['job_id']
            model_id = response_data['model_id']
            # print(f"Processing Request ID: {job_id}. Model ID: {model_id}.")
    except Exception as e:
        logging.error(f"Failed to process response data: {e}")

    return response_data, request_latency

def check_and_reload_model(config, last_signal_time):
    current_time = time.time()
    # Only proceed if it's been at least 600 seconds
    if current_time - last_signal_time >= config.reload_interval:
        model_id = list(config.loaded_loras.keys())[0] if config.loaded_loras else list(config.loaded_models.keys())[0] if config.loaded_models else None
        if model_id is None:
            logging.warning("No loaded models found. Posting to miner_signal to load a new model.")
            # continue to get the next signal
        
        response = post_request(config, config.signal_url + "/miner_signal", {
            "miner_id": config.miner_id,
            "model_type": "SD",
            "version": config.version, # format is like "sd-v1.2.0"
            "options": {"exclude_sdxl": config.exclude_sdxl}
        }, config.miner_id)

        # Process the response only if it's valid
        if response and response.status_code == 200:
            model_id_from_signal = response.json().get('model_id')
            # Proceed if the model is in local storage and not already loaded
            if model_id_from_signal in get_local_model_ids(config) and model_id_from_signal not in config.loaded_models and model_id_from_signal not in config.loaded_loras:
                reload_model(config, model_id_from_signal)
                last_signal_time = current_time  # Update last_signal_time after reloading model
        else:
            logging.error(f"Failed to get a valid response from /miner_signal for miner_id {config.miner_id}.")
    
    # Return the updated or unchanged last_signal_time
    return last_signal_time if current_time - last_signal_time < config.reload_interval else current_time

def parse_log_file(log_file_path):
    device_info_pattern = re.compile(r"INFO - Device (\d+): (.+)")
    job_received_pattern = re.compile(r"INFO - Response from server for miner_id .*: (.+)")
    job_processing_pattern = re.compile(r"INFO - Processing Request ID: (.+). Model ID: (.+).")
    job_completed_pattern = re.compile(r"INFO - Request ID (.+) completed. Total time: ([\d.]+) s")
    latency_pattern = re.compile(r"INFO - Latencies - Request: ([\d.]+) s, Loading: ([\d.]+) s, Inference: ([\d.]+) s, Upload: ([\d.]+) s, Submit: ([\d.]+) s")
 
    devices = {}
    metrics = {
        'gpu_usage': [],
        'num_jobs': 0,
        'success_jobs': 0,
        'failed_jobs': 0,
        'latency': [],
        'jobs_being_processed': 0
    }
 
    with open(log_file_path, 'r') as log_file:
        log_lines = log_file.readlines()
    
    # Find the last occurrence of the run marker
    start_index = next(i for i in reversed(range(len(log_lines))) if RUN_MARKER in log_lines[i]) + 1

    for line in log_lines[start_index:]:
        device_match = device_info_pattern.search(line)
        if device_match:
            device_id = int(device_match.group(1))
            device_name = device_match.group(2)
            devices[device_id] = {
                'Device Name': device_name,
                'Status': 'Idle',
                'Job ID': None,
                'Model ID': None,
                'Total Time': None,
                'Request Latency': None,
                'Loading Latency': None,
                'Inference Latency': None,
                'Upload Latency': None,
                'Submit Latency': None
            }

        if job_received_match := job_received_pattern.search(line):
            try:
                job_response = json.loads(job_received_match.group(1).replace("'", "\""))
            except json.JSONDecodeError:
                continue

        if "Processing Request ID" in line:
            metrics['jobs_being_processed'] += 1
            metrics['num_jobs'] += 1

        # if job_processing_match := job_processing_pattern.search(line):
        #     device_id = int(job_processing_match.group(1))
        #     if device_id in devices:
        #         devices[device_id]['Status'] = 'Processing'
        #         devices[device_id]['Job ID'] = job_processing_match.group(1)
        #         devices[device_id]['Model ID'] = job_processing_match.group(2)

        if job_completed_match := job_completed_pattern.search(line):
            metrics['success_jobs'] += 1
            metrics['jobs_being_processed'] -= 1
            total_time = float(job_completed_match.group(2))
            metrics['latency'].append(total_time)

        if "WARNING" in line:
            metrics['failed_jobs'] += 1
 
    nvmlInit()
    device_count = nvmlDeviceGetCount()
    for i in range(device_count):
        handle = nvmlDeviceGetHandleByIndex(i)
        utilization = nvmlDeviceGetUtilizationRates(handle)
        gpu_usage = utilization.gpu
        metrics['gpu_usage'].append(gpu_usage)
    nvmlShutdown()
 
    avg_latency = sum(metrics['latency']) / len(metrics['latency']) if metrics['latency'] else 0
 
    return {
        'gpu_usage': metrics['gpu_usage'],
        'num_jobs': metrics['num_jobs'],
        'success_jobs': metrics['success_jobs'],
        'failed_jobs': metrics['failed_jobs'],
        'latency': avg_latency,
        'jobs_being_processed': metrics['jobs_being_processed']
    }
 
def display_mining_data(metrics):
    table_data = [
        ["GPU Usage",  metrics['gpu_usage']],
        ["Number of Concurrent Jobs", metrics['num_jobs']],
        ["Successful Jobs", metrics['success_jobs']],
        ["Failed Jobs", metrics['failed_jobs']],
        ["Average Latency", f"{metrics['latency']:.2f} s"],
        ["Jobs Being Processed", metrics['jobs_being_processed']]
    ]
 
    print(tabulate(table_data, tablefmt="grid"))
 
def display_data_thread(log_file_path, display_interval):
    def draw_table(stdscr):
        curses.curs_set(0)  # Hide the cursor
        stdscr.nodelay(1)  # Non-blocking input

        while True:
            metrics = parse_log_file(log_file_path)
            stdscr.clear()
            stdscr.addstr(0, 0, "Mining Data")
            # Initialize the table with the Metric and Value headers
            table_data = [["Metric", "Value"]]

            # Add rows for each GPU usage
            for i, usage in enumerate(metrics['gpu_usage']):
                table_data.append([f"GPU{i} Usage", f"{usage}%"])

            # Add rows for each other metric
            table_data.append(["Number of Concurrent Jobs", metrics['num_jobs']])
            table_data.append(["Successful Jobs", metrics['success_jobs']])
            table_data.append(["Failed Jobs", metrics['failed_jobs']])
            table_data.append(["Average Latency", f"{metrics['latency']:.2f} s"])
            table_data.append(["Jobs Being Processed", metrics['jobs_being_processed']])

            
            # Transpose the table data
            transposed_data = list(zip(*table_data))
            transposed_data = [list(row) for row in transposed_data]

            table = tabulate(transposed_data, tablefmt="grid")

            try:
                max_y, max_x = stdscr.getmaxyx()
                if len(table.splitlines()) + 1 > max_y or len(table.splitlines()[0]) > max_x:
                    # Handle the case where the table is too large for the screen
                    stdscr.addstr(1, 0, "Screen too small for table display")
                else:
                    stdscr.addstr(1, 0, table)
            except curses.error:
                pass  # Ignore curses errors for now

            stdscr.refresh()
            time.sleep(display_interval)

            # Check for user input to exit
            try:
                if stdscr.getch() == ord('q'):
                    break
            except curses.error:
                pass

    curses.wrapper(draw_table)
 
def process_jobs(config):
    current_model_id = next(iter(config.loaded_models), None)
    current_lora_id = next(iter(config.loaded_loras), None)
    model_ids = get_local_model_ids(config)
    if not model_ids:
        logging.debug("No models found. Exiting...")
        sys.exit(0)

    model_id_to_send = current_lora_id if current_lora_id is not None else current_model_id
    job, request_latency = send_miner_request(config, model_id_to_send, config.min_deadline)
    if not job:
        logging.info("No job received.")
        return False

    job_start_time = time.time()
    logging.info(f"Processing Request ID: {job['job_id']}. Model ID: {job['model_id']}.")
    submit_job_result(config, config.miner_id, job, job['temp_credentials'], job_start_time, request_latency)
    return True

def main(cuda_device_id):

    torch.cuda.set_device(cuda_device_id)
    config = load_config(cuda_device_id=cuda_device_id)
    config = initialize_logging_and_args(config, cuda_device_id, miner_id=config.miner_id)

    # The parent process should have already downloaded the model files
    # Now we just need to load them into memory
    fetch_and_download_config_files(config)

    # Load the default model before entering the loop
    load_default_model(config)

    last_signal_time = time.time()
    while True:
        try:
            last_signal_time = check_and_reload_model(config, last_signal_time)
            executed = process_jobs(config)
        except Exception as e:
            logging.error("Error occurred:", exc_info=True)
            executed = False
        if not executed:
            time.sleep(config.sleep_duration)
            
if __name__ == "__main__":
    processes = []
    def signal_handler(signum, frame):
        for p in processes:
            p.terminate()
        curses.endwin()  # Ensure curses is terminated
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    set_start_method('spawn', force=True)
    
    config = load_config()
    config = initialize_logging_and_args(config, miner_id=config.miner_id)

    if config.num_cuda_devices > torch.cuda.device_count():
        print("Number of CUDA devices specified in config is greater than available. Exiting...")
        sys.exit(1)
    check_cuda()

    fetch_and_download_config_files(config)

    # Initialize and start model updater before processing tasks
    model_updater = ModelUpdater(config=config.__dict__)  # Assuming config.__dict__ provides necessary settings
    if not config.skip_checksum:
        model_updater.compare_model_checksums()
    # Start the model updater in a separate thread
    updater_thread = threading.Thread(target=model_updater.start_scheduled_updates)
    updater_thread.start()

    # Write the marker indicating the start of a new run
    logging.info(RUN_MARKER)

    display_interval = 10  # Display interval in seconds
    display_thread = threading.Thread(target=display_data_thread, args=('./sd-miner_0_0x1c83C85b57117E73f1195c37316b2E99B481aD6e-7bac77.log', display_interval))
    display_thread.start()
 
    
    # TODO: There appear to be 1 leaked semaphore objects to clean up at shutdown
    # Launch a separate process for each CUDA device
    try:
        for i in range(config.num_cuda_devices):
            p = Process(target=main, args=(i,))
            p.start()
            processes.append(p)

        for p in processes:
            p.join()

    except KeyboardInterrupt:
        print("Main process interrupted. Terminating child processes.")
        for p in processes:
            p.terminate()
            p.join()
            curses.endwin() # Ensure curses is terminated
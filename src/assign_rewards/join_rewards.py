"""
AWS Batch worker
This script is intended to be used from inside a Docker container to process
jsonl files.
-------------------------------------------------------------------------------
Usage:
    python join_rewards.py
"""

# Built-in imports
from datetime import timedelta
from datetime import datetime
import logging
import json
import gzip
import sys
import shutil
import signal

# Local imports
from utils import load_records
from utils import name_no_ext
from utils import sort_records_by_timestamp
from config import OUTPUT_FILENAME
from config import DEFAULT_REWARD_KEY
from config import DATETIME_FORMAT
from config import DEFAULT_EVENTS_REWARD_VALUE
from config import DEFAULT_REWARD_VALUE
from config import INCOMING_PATH
from config import HISTORIES_PATH
from config import LOGGING_LEVEL
from config import LOGGING_FORMAT
from config import REWARD_WINDOW
from config import AWS_BATCH_JOB_ARRAY_INDEX
from config import REWARD_ASSIGNMENT_WORKER_COUNT
from exceptions import InvalidTypeError
from exceptions import UpdateListenersError

# Setup logging
logging.basicConfig(format=LOGGING_FORMAT, level=LOGGING_LEVEL)

# Time window to add to a timestamp
window = timedelta(seconds=REWARD_WINDOW)

SIGTERM = False

def worker():
    """
    Identify the relevant folders that this worker should process, identify 
    the files that need to be processed and write the gzipped results.
    """

    print(f"Starting AWS Batch Array job.")

    node_id       = AWS_BATCH_JOB_ARRAY_INDEX
    node_count    = REWARD_ASSIGNMENT_WORKER_COUNT

    files_to_process = identify_files_to_process(INCOMING_PATH, node_id, node_count)

    print(
        f"This instance (node {node_id}) will process the files: "
        f"{', '.join([f.name for f in files_to_process])}")

    # for f in files_to_process:
    #     handle_signals()
    #     decision_records, reward_records, event_records = load_records(str(f))
    #     rewarded_records = assign_rewards_to_decisions(decision_records, reward_records, event_records)
    #     gzip_records(f, rewarded_records)

    print(f"AWS Batch Array (node {node_id}) finished.")


def update_listeners(listeners, record_timestamp, reward):
    """
    Update the reward property value of each of the given list of records, 
    in place.
    
    Args:
        listeners        : list of dicts
        record_timestamp : A datetime.datetime object
        reward           : int or float
    
    Returns:
        None
    
    Raises:
        TypeError if an unexpected type is received
    """

    if not isinstance(listeners, list):
        raise TypeError("Expecting a list for the 'listeners' arg.")

    if not isinstance(record_timestamp, datetime):
        raise TypeError("Expecting a datetime.datetime timestamp for the 'record_timestamp' arg.")

    if not (isinstance(reward, int) or isinstance(reward, float)):
        raise TypeError("Expecting int, float or bool.")

    try:

        # Loop backwards to be able to remove an item in place
        for i in range(len(listeners)-1, -1, -1):
            listener = listeners[i]
            listener_timestamp = datetime.strptime(listener['timestamp'], DATETIME_FORMAT)
            if listener_timestamp + window < record_timestamp:
                print(f'Deleting listener: {listener_timestamp}, Reward/event: {record_timestamp}')
                del listeners[i]
            else:
                print(f'Adding reward of {float(reward)} to decision.')
                listener['reward'] = listener.get('reward', DEFAULT_REWARD_VALUE) + float(reward)

    except Exception as e:
        raise UpdateListenersError


def assign_rewards_to_decisions(decision_records, reward_records, event_records):
    """
    1) Collect all records of type "decision" in a dictionary.
    2) Assign the rewards of records of type "rewards" to all the "decision" 
    records that match two criteria:
      - reward_key
      - a time window
    3) Assign the value of records of type "event" to all "decision" records
    within a time window.

    Args:
        records: a list of records (dicts) sorted by their "timestamp" property.

    Returns:
        dict whose keys are 'reward_key's and its values are lists of records.
    
    Raises:
        InvalidTypeError: If a record has an invalid type attribute.
    """

    records = []
    records.extend(decision_records)
    records.extend(reward_records)
    records.extend(event_records)
    
    sort_records_by_timestamp(records)

    decision_records_by_reward_key = {}
    for record in records:
        if record.get('type') == 'decision':
            reward_key = record.get('reward_key', DEFAULT_REWARD_KEY)
            listeners = decision_records_by_reward_key.get(reward_key, [])
            decision_records_by_reward_key[reward_key] = listeners
            listeners.append(record)
        
        elif record.get('type') == 'rewards':
            record_timestamp = datetime.strptime(record['timestamp'], DATETIME_FORMAT)
            for reward_key, reward in record['rewards'].items():
                listeners = decision_records_by_reward_key.get(reward_key, [])
                update_listeners(listeners, record_timestamp, reward)
        
        # Event type records get summed to all decisions within the time window regardless of reward_key
        elif record.get('type') == 'event':
            reward = record.get('properties', { 'properties': {} }) \
                           .get('value', DEFAULT_EVENTS_REWARD_VALUE)
            record_timestamp = datetime.strptime(record['timestamp'], DATETIME_FORMAT)
            for reward_key, listeners in decision_records_by_reward_key.items():
                update_listeners(listeners, record_timestamp, reward)
            
        else:
            raise InvalidTypeError
    
    return decision_records


def gzip_records(input_file, rewarded_records):
    """
    Create a gzipped jsonlines file with the rewarded decision records.
    Create a parent subdir if doesn't exist (e.g. dir "aa" in aa/file.jsonl.gz)
    
    Args:
        input_file      : Path object towards the input jsonl file (only for 
                          the purpose of extracting its name)
        rewarded_records: A list of rewarded records

    """

    output_subdir =  PATH_OUTPUT_DIR / input_file.parents[0].name

    if not output_subdir.exists():
        print(f"Folder '{str(output_subdir)}' doesn't exist. Creating.")
        output_subdir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_subdir / OUTPUT_FILENAME.format(input_file.name)

    try:
        if output_file.exists():
            print(f"Overwriting output file '{output_file}'")
        else:
            print(f"Writing new output file '{output_file.absolute()}'")
        
        with gzip.open(output_file.absolute(), mode='wt') as gzf:
            for record in rewarded_records:
                gzf.write((json.dumps(record) + "\n"))
    
    except Exception as e:
        logging.error(
            f'An error occurred while trying to write the gzipped file:'
            f'{str(output_file)}'
            f'{e}')
        raise e

def identify_files_to_process(input_dir, node_id, node_count):
    """
    Return a list of Path objects representing files that need to be processed.

    Args:
        input_dir : Path object towards the input folder.
        node_id   : int representing the id of the target node (zero-indexed)
        node_count: int representing the number of total nodes of the cluster
    
    Returns:
        List of Path objects representing files
    """

    files_to_process = []
    for f in input_dir.glob('*.jsonl.gz'):
        # convert first 16 hex chars (64 bit) to an int
        # the file name starts with a sha-256 hash so the bits will be random
        # check if int mod node_count matches our node
        if (int(f.name[:16], 16) % node_count) == node_id:
            files_to_process.append(f)

    return files_to_process


def handle_signals():
    if SIGTERM:
        print(f'Quitting due to SIGTERM signal (node {AWS_BATCH_JOB_ARRAY_INDEX}).')
        sys.exit()


def signal_handler(signalNumber, frame):
    global SIGTERM
    SIGTERM = True
    print(f"SIGTERM received (node {AWS_BATCH_JOB_ARRAY_INDEX}).")
    return


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    worker()
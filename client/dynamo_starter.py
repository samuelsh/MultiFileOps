#!/usr/bin/env python3.6

import argparse
import errno
import json
import multiprocessing
import os
import traceback
import time
import sys
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Process
from async_controller import Controller
from generic_mounter import Mounter
from dynamo import Dynamo
from logger import pubsub_logger
from config import MAX_WORKERS_PER_CLIENT, FILE_NAMES_PATH, DYNAMO_PATH
from tree import dirtree

stop_event = multiprocessing.Event()


def futures_validator(futures, logger):
    """

    :param logger:
    :param futures: list
    :return: None
    """
    for future in futures:
        try:
            future.result()
        except Exception as e:
            logger.error(f"Future raised exception: {e}")
            raise e


def load_config():
    with open(os.path.join(os.path.expanduser(DYNAMO_PATH), "server", "config.json")) as f:
        test_config = json.load(f)
    return test_config


def run_worker(mount_points, controller, server, nodes, domains, **kwargs):
    worker = Dynamo(mount_points, controller, server, nodes, domains, **kwargs)
    worker.run()


def get_args():
    """
    Supports the command-line arguments listed below.
    """

    parser = argparse.ArgumentParser(
        description='Test Runner script')
    parser.add_argument('-c', '--controller', type=str, required=True, help='Controller host name')
    parser.add_argument('-s', '--server', type=str, required=True, help='Cluster Server hostname')
    parser.add_argument('-e', '--export', type=str, help='NFS Export Name', default="/")
    parser.add_argument('-n', '--nodes', type=int, help='Number of active nodes', default=0)
    parser.add_argument('-d', '--domains', type=int, help='Number of fs domains', default=0)
    parser.add_argument('-m', '--mtype', type=str, help='Mount Type', choices=['nfs3', 'nfs4', 'nfs4.1', 'smb1', 'smb2',
                                                                               'smb3'], default="nfs3")
    parser.add_argument('--start_vip', type=str, help="Start VIP address range")
    parser.add_argument('--end_vip', type=str, help="End VIP address range")
    parser.add_argument('-l', '--locking', type=str, help='Locking Type', choices=['native', 'application', 'off'],
                        default="native")
    args = parser.parse_args()
    return args


def run_controller(event, dir_tree, test_config, clients_ready_event):
    Controller(event, dir_tree, test_config, clients_ready_event).run()


def run():
    file_names = None
    args = get_args()
    logger = pubsub_logger.PUBLogger(args.controller).logger
    clients_ready_event = multiprocessing.Event()
    dir_tree = dirtree.DirTree(file_names)
    test_config = load_config()
    try:
        with open(FILE_NAMES_PATH, 'r') as f:  # If file with names isn't exists, we'll just create random files
            file_names = f.readlines()
    except IOError as io_error:
        if io_error.errno == errno.ENOENT:
            pass
    time.sleep(10)
    try:
        os.chdir(os.path.join(os.path.expanduser('~'), 'qa', 'dynamo', 'client'))
        logger.info("Mounting work path...")
        mounter = Mounter(args.server, args.export, args.mtype, 'VFS_STRESS', logger=logger, nodes=args.nodes,
                          domains=args.domains, sudo=True, start_vip=args.start_vip, end_vip=args.end_vip)
        try:
            mounter.mount_all_vips()
        except AttributeError:
            logger.warn("VIP range is bad or None. Falling back to mounting storage server IP")
            mounter.mount()
    except Exception as error_on_init:
        logger.error(str(error_on_init) + " WorkDir: {0}".format(os.getcwd()))
        raise
    # Start a few worker processes
    logger.info("Starting controller")
    controller_process = Process(target=run_controller, args=(stop_event, dir_tree, test_config, clients_ready_event))
    controller_process.start()
    futures = []
    with ProcessPoolExecutor(MAX_WORKERS_PER_CLIENT) as executor:
        for i in range(MAX_WORKERS_PER_CLIENT):
            futures.append(executor.submit(run_worker, mounter.mount_points, args.controller, args.server, args.nodes,
                                           args.domains, **dict(locking_type=args.locking)))
        time.sleep(10)
        clients_ready_event.set()
    futures_validator(futures, logger)
    logger.info('all done')
    controller_process.join()


if __name__ == '__main__':
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

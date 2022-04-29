#!/usr/bin/env python3
"""
Main file for dealing with connecting to MakeMKV and handling errors
"""
import sys
import os
import logging
import subprocess
import shlex

from arm.config.config import cfg
from arm.ripper import utils  # noqa: E402
from arm.ui import db  # noqa: F401, E402


class MakeMkvRuntimeError(RuntimeError):
    """Exception raised when a CalledProcessError is thrown during execution of a `makemkvcon` command.

    Attributes:
        message: the explanation of the error
    """

    def __init__(self, error):
        self.message = f"Call to MakeMKV failed with code: {error.returncode} ({error.output})"
        logging.error(self.message)
        raise super().__init__(self.message)


def makemkv(logfile, job):
    """
    Rip Blu-rays/DVDs with MakeMKV\n\n

    :param logfile: Location of logfile to redirect MakeMKV logs to
    :param job: job object
    :return: path to ripped files.
    """

    # confirm MKV is working, beta key hasn't expired
    prep_mkv(job)
    logging.info(f"Starting MakeMKV rip. Method is {cfg['RIPMETHOD']}")
    # get MakeMKV disc number
    logging.debug("Getting MakeMKV disc number")
    cmd = f"makemkvcon -r info disc:9999  |grep {job.devpath} |grep -oP '(?<=:).*?(?=,)'"
    try:
        mdisc = subprocess.check_output(
            cmd,
            shell=True
        ).decode("utf-8")
        logging.info(f"MakeMKV disc number: {mdisc.strip()}")
    except subprocess.CalledProcessError as mdisc_error:
        raise MakeMkvRuntimeError(mdisc_error)

    # get filesystem in order
    rawpath = setup_rawpath(job, os.path.join(str(cfg["RAW_PATH"]), str(job.title)))

    # Rip bluray
    if cfg["RIPMETHOD"] == "backup" and job.disctype == "bluray":
        # backup method
        cmd = f'makemkvcon backup --minlength={cfg["MINLENGTH"]} --decrypt {cfg["MKV_ARGS"]} ' \
              f'-r disc:{mdisc.strip()} {shlex.quote(rawpath)}>> {logfile}'
        logging.info("Backup up disc")
        run_makemkv(cmd)
    # Rip DVD
    elif cfg["RIPMETHOD"] == "mkv" or job.disctype == "dvd":
        get_track_info(mdisc, job)

        # if no maximum length, process the whole disc in one command
        if int(cfg["MAXLENGTH"]) > 99998:
            cmd = f'makemkvcon mkv {cfg["MKV_ARGS"]} -r --progress=-stdout --messages=-stdout ' \
                  f'dev:{job.devpath} all {shlex.quote(rawpath)} --minlength={cfg["MINLENGTH"]}>> {logfile}'
            run_makemkv(cmd)
        else:
            process_single_tracks(job, logfile, rawpath)
    else:
        logging.info("I'm confused what to do....  Passing on MakeMKV")

    job.eject()
    logging.info(f"Exiting MakeMKV processing with return value of: {rawpath}")
    return rawpath


def process_single_tracks(job, logfile, rawpath):
    """
    For processing single tracks from MakeMKV one at a time
    :param job: job object
    :param str logfile: path of logfile
    :param str rawpath:
    :return:
    """
    # process one track at a time based on track length
    for track in job.tracks:
        if track.length < int(cfg["MINLENGTH"]):
            # too short
            logging.info(f"Track #{track.track_number} of {job.no_of_titles}. Length ({track.length}) "
                         f"is less than minimum length ({cfg['MINLENGTH']}).  Skipping")
        elif track.length > int(cfg["MAXLENGTH"]):
            # too long
            logging.info(f"Track #{track.track_number} of {job.no_of_titles}. "
                         f"Length ({track.length}) is greater than maximum length ({cfg['MAXLENGTH']}).  "
                         "Skipping")
        else:
            # just right
            logging.info(f"Processing track #{track.track_number} of {(job.no_of_titles - 1)}. "
                         f"Length is {track.length} seconds.")
            filepathname = os.path.join(rawpath, track.filename)
            logging.info(f"Ripping title {track.track_number} to {shlex.quote(filepathname)}")

            cmd = f'makemkvcon mkv {cfg["MKV_ARGS"]} -r --progress=-stdout --messages=-stdout' \
                  f'dev:{job.devpath} {track.track_number} {shlex.quote(rawpath)} ' \
                  f'--minlength={cfg["MINLENGTH"]}>> {logfile}'
            # Possibly update db to say track was ripped
            run_makemkv(cmd)


def setup_rawpath(job, raw_path):
    """
    Checks if we need to create path and does so if needed\n\n
    :param job:
    :param raw_path:
    :return: raw_path
    """

    logging.info(f"Destination is {raw_path}")
    if not os.path.exists(raw_path):
        try:
            os.makedirs(raw_path)
        except OSError:
            err = f"Couldn't create the base file path: {raw_path} Probably a permissions error"
            logging.debug(err)
    else:
        logging.info(f"{raw_path} exists.  Adding timestamp.")
        random_time = job.stage
        raw_path = os.path.join(str(cfg["RAW_PATH"]), f"{job.title}_{random_time}")
        logging.info(f"raw_path is {raw_path}")
        try:
            os.makedirs(raw_path)
        except OSError:
            err = f"Couldn't create the base file path: {raw_path} Probably a permissions error"
            sys.exit(err)
    return raw_path


def prep_mkv(job):
    """Make sure the MakeMKV key is up-to-date

    Parameters:
        job: job object\n
    Raises:
        MakeMkvRuntimeException
    """
    logging.info("Prepping MakeMkv for usage...")

    cmd = f"makemkvcon info {job.devpath}"
    try:
        # check=True is needed to make the exception throw on a non-zero return
        subprocess.run(cmd, capture_output=True, shell=True, check=True)  # noqa: F841
    except subprocess.CalledProcessError as mkv_error:
        if mkv_error.returncode == 253:
            # MakeMKV is out of date
            logging.info("MakeMKV: return code is 253, MakeMKV beta key has expired.")
            update_key()
            try:
                subprocess.run(cmd, capture_output=True, shell=True, check=True)  # noqa: F841
            except subprocess.CalledProcessError as mkv_redux_error:
                if mkv_redux_error.returncode == 10:
                    logging.info("MakeMKV beta key updated successfully!")
                else:
                    raise MakeMkvRuntimeError(mkv_redux_error)
        elif mkv_error.returncode == 10:
            # For some fucking reason the nominal return value for `makemkvcon info` is 10
            logging.info("MakeMKV is working as expected!")
        else:
            raise MakeMkvRuntimeError(mkv_error)


def update_key():
    """Run a script to update the MakeMKV beta key after it expires.

    Raises:
        RuntimeError
    """
    try:
        logging.info("Updating MakeMKV key...")
        update_cmd = "/bin/bash /opt/arm/scripts/update_key.sh"
        subprocess.run(update_cmd, capture_output=True, shell=True, check=True)  # noqa: F841
    except subprocess.CalledProcessError as update_err:
        err = f"Error updating MakeMKV key, return code: {update_err.returncode}"
        logging.error(err)
        raise RuntimeError(err)


def get_track_info(mdisc, job):
    """
    Use MakeMKV to get track info and update Track class

    :param mdisc: MakeMKV disc number
    :param job: Job instance
    :return: None

    .. note:: For help with MakeMKV codes: https://github.com/1337-server/automatic-ripping-machine/wiki/MakeMKV-Codes
    """

    logging.info("Using MakeMKV to get information on all the tracks on the disc. This will take a few minutes...")

    cmd = f'makemkvcon -r --progress=-stdout --messages=-stdout --minlength={cfg["MINLENGTH"]} ' \
          f'--cache=1 info disc:{mdisc}'
    logging.debug(f"Sending command: {cmd}")
    try:
        mkv = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            shell=True
        ).decode("utf-8").splitlines()
    except subprocess.CalledProcessError as mdisc_error:
        raise MakeMkvRuntimeError(mdisc_error) from mdisc_error

    track = 0
    fps = float(0)
    aspect = ""
    seconds = 0
    filename = ""
    for line in mkv:
        # MSG:3028 - track was added (contains total length and chapter length)
        # MSG:3025 - too short - track was skipped
        # MSG:2003 - read error
        if line.split(":")[0] in ("MSG", "TCOUNT", "CINFO", "TINFO", "SINFO"):
            line_split = line.split(":", 1)
            msg_type = line_split[0]
            msg = line_split[1].split(",")
            line_track = int(msg[0])
            # Total track count
            if msg_type == "TCOUNT":
                logging.info(f"Found {line_split[1].strip()} titles")
                utils.database_updater({'no_of_titles': int(line_split[1].strip())}, job)
            # Title info add track and get filename
            if msg_type == "TINFO":
                filename, track = add_track_filename(aspect, filename, fps, job,
                                                     line_track, msg, seconds, track)
            # Title length
            seconds = find_track_length(msg, msg_type, seconds)
            # Aspect ratio and fps
            aspect, fps = find_aspect_fps(aspect, msg, msg_type, fps)
    # If we haven't already added any tracks add one with what we have
    utils.put_track(job, track, seconds, aspect, fps, False, "MakeMKV", filename)


def find_track_length(msg, msg_type, seconds):
    """
    Find the track length from TINFO msg from MakeMKV\n
    :param msg: current MakeMKV line split by ','
    :param msg_type: the message type from MakeMKV
    :param seconds: length in seconds of file
    :return: seconds of file
    """
    if msg_type == "TINFO" and msg[1] == "9":
        len_hms = msg[3].replace('"', '').strip()
        hour, mins, secs = len_hms.split(':')
        seconds = int(hour) * 3600 + int(mins) * 60 + int(secs)
    return seconds


def find_aspect_fps(aspect, msg, msg_type, fps):
    """
    Search current line and find the file's aspect ratio and fps if msg_type is SINFO\n
    :param str aspect: aspect ratio (stored as float but db wants string)
    :param msg: current MakeMKV line split by ','
    :param msg_type: the message type from MakeMKV
    :param float fps: fps of file
    :return: [aspect, fps]

    .. note::
           aspect.msg - ['0', '0', '20', '0', '"16:9"']\n
           fps.msg - ['0', '0', '21', '0', '"25"']\n
    """
    if msg_type == "SINFO" and msg[1] == "0":
        if msg[2] == "20":
            # aspect comes wrapped in "" remove them
            aspect = msg[4].replace('"', '').strip()
        elif msg[2] == "21":
            fps = msg[4].split()[0]
            fps = fps.replace('"', '').strip()
            fps = float(fps)
    return aspect, fps


def add_track_filename(aspect, filename, fps, job, line_track, msg, seconds, track):
    """
    Only add tracks that weren't previously added ? Also finds filename and removes quotes around it\n
    :param aspect: Aspect ratio of file
    :param filename: Filename of file
    :param fps: FPS of file
    :param job: Job the track belongs to
    :param line_track: e.g TINFO: **3** ,8,0,"2"
    :param msg: current line from MakeMKV split into array
    :param int seconds: Length of track
    :param track: Track number of current file
    :return: [filename, track]
    """
    if track != line_track:
        if line_track != int(0):
            utils.put_track(job, track, seconds, aspect, fps, False, "MakeMKV", filename)
        track = line_track
    if msg[1] == "27":
        filename = msg[3].replace('"', '').strip()
    return filename, track


def run_makemkv(cmd):
    """
    Run MakeMKV with the command passed to the function.

    Parameters:
        cmd: the command to be run
    Raises:
        MakeMkvRuntimeError
    """

    logging.debug(f"Ripping with the following command: {cmd}")
    try:
        subprocess.run(cmd, capture_output=True, shell=True, check=True)
    except subprocess.CalledProcessError as mkv_error:
        raise MakeMkvRuntimeError(mkv_error) from mkv_error

# -*- encoding: utf-8 -*-
# Copyright (c) 2013 Hannes Körber <hannes.koerber+rbackupd@gmail.com>
#
# This file is part of rbackupd.
#
# rbackupd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rbackupd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import logging
import os
import re
import sys

from . import cron
from . import constants as const
from . import rsync
from . import files

logger = logging.getLogger(__name__)


class Repository(object):
    """
    Represents a repository of backups, which means a collection of backups
    that are managed together. Provides methods to determine whether new
    backups are necessary and get expired backups.
    """

    def __init__(self, sources, destination, name, intervals, keep, keep_age,
                 rsyncfilter, rsync_logfile_options, rsync_args, rsync_cmd):
        self.sources = sources
        self.destination = destination
        self.name = name
        self.intervals = \
            [BackupInterval(interval_name, cron.Cronjob(interval)) for
             (interval_name, interval) in intervals.items()]
        self.keep = keep
        self.keep_age = keep_age
        self.rsyncfilter = rsyncfilter
        self.rsync_logfile_options = rsync_logfile_options
        self.rsync_args = rsync_args
        self.rsync_cmd = rsync_cmd
        self._backups = self._read_backups()

    @property
    def backups(self):
        assert(self._backups is not None)
        return self._backups

    def _read_backups(self):
        logger.debug("Repository \"%s\": Reading backups.", self.name)
        backups = []
        for folder in os.listdir(self.destination):
            if folder == const.SYMLINK_LATEST_NAME:
                logger.debug("Repository \"%s\": Ignoring latest symlink "
                             "\"%s\".", self.name, folder)
                continue
            backups.append(BackupFolder(os.path.join(self.destination,
                                                     folder)))
        logger.debug("Repository \"%s\": Found the following folders: %s.",
                     self.name,
                     backups)
        backups = [backup for backup in backups if backup.is_finished()]
        for backup in backups:
            if not backup.is_finished():
                logger.warning("Backup \"%s\" is not recognized as a valid "
                               "backup, will be skipped.",
                               backup.folder_name)
                del backup
        for backup in backups:
            backup.read_meta_file()
        return backups

    def _register_backup(self, backup):
        assert(self._backups is not None)
        logger.debug("Repository \"%s\": Registering backup \"%s\".",
                     self.name, backup.name)
        self._backups.append(backup)

    def _unregister_backup(self, backup):
        assert(self._backups is not None)
        if backup not in self._backups:
            raise ValueError("backup not found")
        logger.debug("Repository \"%s\": Unregistering backup \"%s\".",
                     self.name, backup.name)
        self._backups.remove(backup)

    def get_necessary_intervals(self):
        """
        Returns all backups deemed necessary.
        :returns: A list of tuples containing all necessary backups, each tuple
        consisting of the interval name as first and the interval cron object
        as second element.
        :returns: All backups deemed necessary.
        :rtype: list of tuples.
        """
        necessary_backups = []
        for interval in self.intervals:
            logger.debug("Repository \"%s\": Checking interval \"%s\" for "
                         "necessary backups.",
                         self.name,
                         interval.name)
            latest_backup = self._get_latest_backup_of_interval(interval.name)
            if latest_backup is None:
                logger.debug("Repository \"%s\": Backup necessary as no other "
                             "backups of that interval are present.",
                             self.name)
                necessary_backups.append(interval)
                continue
            if interval.cronjob.has_occured_since(latest_backup.date,
                                                  include_start=False):
                logger.debug("Repository \"%s\": Backup necessary as interval "
                             "occured since latest backup.",
                             self.name)
                necessary_backups.append(interval)
        return necessary_backups

    def get_backup_params(self, new_backup_interval_name):
        """
        Gets the parameters for a backup of the specific interval with a given
        timestamp.
        :param new_backup_interval_name: The interval name of the new backup.
        :type new_backup_interval_name: string
        :returns: A BackupParameters instance with information about the new
        backup.
        :rtype: BackupParameters instance
        """
        logger.debug("Repository \"%s\": Getting parameters of new backup.",
                     self.name)
        new_link_ref = self._get_latest_backup()
        logger.debug("Link-ref of new backup: \"%s\"", new_link_ref)
        backup_params = BackupParameters(
            link_ref=new_link_ref,
            rsync_cmd=self.rsync_cmd,
            rsync_args=self.rsync_args,
            rsync_filter=self.rsyncfilter,
            rsync_logfile_options=self.rsync_logfile_options)
        return backup_params

    def create_backups_if_necessary(self, timestamp):
        """
        Checks whether a backups are necessary and creates them.
        :param timestamp: The timestamp all potential backups created will be
        assigned.
        :type timestamp: datetime.datetime instance
        """
        necessary_intervals = self.get_necessary_intervals()
        if len(necessary_intervals) == 0:
            logger.verbose("No backup necessary.")
            return

        interval = necessary_intervals[0]

        new_folder_name = const.PATTERN_BACKUP_FOLDER % (
            self.name,
            timestamp.strftime(const.DATE_FORMAT),
            interval.name)

        new_backup = BackupFolder(os.path.join(
            self.destination, new_folder_name))

        params = self.get_backup_params(interval)
        new_backup.set_meta_data(name=new_folder_name,
                                 date=timestamp,
                                 interval=interval.name)
        new_backup.prepare()
        self.create_backup(new_backup, params)
        new_backup.write_meta_file()
        self._register_backup(new_backup)

        for interval in necessary_intervals[1:]:
            symlink_name = const.PATTERN_BACKUP_FOLDER % (
                self.name,
                timestamp.strftime(const.DATE_FORMAT),
                interval.name)

            symlink_backup = BackupFolder(os.path.join(
                self.destination, symlink_name))
            symlink_backup.set_meta_data(name=symlink_name,
                                         date=timestamp,
                                         interval=interval.name)
            symlink_backup.prepare()
            self._symlink_backup_subfolders(new_backup, symlink_backup)
            symlink_backup.write_meta_file()
            self._register_backup(symlink_backup)

    def create_backup(self, new_backup, params):
        destination = new_backup.backup_path
        for source in self.sources:
            if params.link_ref is None:
                link_dest = None
            else:
                link_dest = params.link_ref.backup_path
            logger.info("Creating backup \"%s\".", new_backup.name)
            (returncode, stdoutdata, stderrdata) = rsync.rsync(
                command=params.rsync_cmd,
                source=source,
                destination=destination,
                link_ref=link_dest,
                arguments=params.rsync_args,
                rsyncfilter=params.rsync_filter,
                loggingOptions=params.rsync_logfile_options)
            if returncode != 0:
                logger.critical("Rsync failed. Aborting. Stderr:\n%s",
                                stderrdata)
                sys.exit(const.EXIT_RSYNC_FAILED)
            else:
                logger.debug("Rsync finished successfully.")
        new_backup.write_meta_file()
        logger.info("Backup finished successfully.")
        self._relink_latest_symlink(new_backup)

    def get_expired_backups(self, timestamp):
        """
        Returns all backups that are expired in the repository.
        :returns: All expired backups.
        :rtype: list of Backup instances
        """
        # we will sort the folders and just loop from oldest to newest until we
        # have enough expired backups.
        expired_backups = []
        for interval in self.intervals:
            logger.debug("Repository \"%s\": Checking interval \"%s\" for "
                         "expired backups.",
                         self.name,
                         interval.name)

            if interval.name not in self.keep:
                logger.critical("No corresponding interval found for keep "
                                "value \"%s\"", interval.name)
                sys.exit(9)

            if interval.name not in self.keep_age:
                logger.critical("No corresponding age interval found of keep "
                                "value \"%s\"", interval.name)
                sys.exit(10)

            backups_of_that_interval = [backup for backup in self.backups if
                                        backup.interval_name == interval.name]

            expired_backups.extend(
                self._get_expired_backups_by_count(backups_of_that_interval,
                                                   self.keep[interval.name]))
            expired_backups.extend(
                self._get_expired_backups_by_age(backups_of_that_interval,
                                                 self.keep_age[interval.name],
                                                 timestamp))

        return expired_backups

    def handle_expired_backups(self, timestamp):
        """
        Gets and handles all expired backups. Removes expired backups and takes
        care about all pending symlinks.

        """
        expired_backups = self.get_expired_backups(timestamp=timestamp)
        if len(expired_backups) == 0:
            logger.verbose("No expired backups.")
            return

        for expired_backup in expired_backups:
            # as a backup might be a symlink to another backup, we have to
            # consider: when it is a symlink, just remove the symlink. if not,
            # other backupSSS!! might be a symlink to it, so we have to check
            # all other backups. we overwrite one symlink with the backup and
            # update all remaining symlinks
            logger.info("Expired backup: \"%s\".",
                        expired_backup.name)
            if os.path.islink(expired_backup.backup_path):
                logger.info("Removing backup containing symlink \"%s\".",
                            expired_backup.name)
                files.remove_recursive(expired_backup.path)
                self._unregister_backup(expired_backup)
            else:
                symlinks = []
                for backup in self.backups:
                    if (os.path.samefile(expired_backup.backup_path,
                                         os.path.realpath(backup.backup_path))
                            and os.path.islink(backup.backup_path)):
                        symlinks.append(backup)

                if len(symlinks) == 0:
                    # just remove the backups, no symlinks present
                    logger.info("Removing directory \"%s\".",
                                expired_backup.name)
                    files.remove_recursive(expired_backup.path)
                    self._unregister_backup(expired_backup)
                else:
                    # replace the first symlink with the backup
                    logger.info("Removing symlink \"%s\".",
                                symlinks[0].name)
                    files.remove_symlink(symlinks[0].backup_path)

                    # move the real backup over
                    logger.info("Moving \"%s\" to \"%s\".",
                                expired_backup.name,
                                symlinks[0].name)
                    files.move(expired_backup.backup_path,
                               symlinks[0].backup_path)

                    # remote the "rest" of the expired backup
                    files.remove_recursive(expired_backup.path)
                    self._unregister_backup(expired_backup)

                    new_real_backup = symlinks[0]
                    # now update all symlinks to the directory
                    for remaining_symlink in symlinks[1:]:
                        logger.info("Removing symlink \"%s\".",
                                    remaining_symlink.backup_path)
                        files.remove_symlink(remaining_symlink.backup_path)
                        self._symlink_backup_subfolders(new_real_backup,
                                                        remaining_symlink)
            logger.info("Backup removed successfully.")

    def _symlink_backup_subfolders(self, target_backup, link_backup):
        link_name = link_backup.backup_path
        target = target_backup.backup_path
        logger.info("Creating symlink \"%s\" pointing to \"%s\"",
                    link_name,
                    target)
        files.create_symlink(target, link_name)

    def _relink_latest_symlink(self, backup):
        destination = backup.path
        logger.debug("Fixing latest symlink, new target is \"%s\".",
                     destination)
        symlink_latest = os.path.join(self.destination,
                                      const.SYMLINK_LATEST_NAME)
        if os.path.islink(symlink_latest):
            files.remove_symlink(symlink_latest)
        files.create_symlink(destination, symlink_latest)
        logger.debug("Latest symlink fixed.")

    def _get_expired_backups_by_count(self, backups, max_count):
        """
        Returns all backups that are expired relative to the maximum count of
        kept backups. Practically, just returns all backups except the
        "max_count" oldest.
        """
        expired_backups = []
        count = len(backups) - max_count
        backups.sort(key=lambda backup: backup.date, reverse=False)
        for i in range(0, count):
            logger.debug("Backup \"%s\" is expired because count %s is "
                         "exceeded.",
                         backups[i].name,
                         max_count)
            expired_backups.append(backups[i])
        return expired_backups

    def _get_expired_backups_by_age(self, backups, max_age, timestamp):
        """
        Returns all backups that are expired relative to the maximum age of
        kept backups. It pracically just returns all backups older than
        max_age.
        """
        expired_backups = []
        for backup in backups:
            if backup.date < max_age:
                logger.debug("Backup \"%s\" expired because it is older than "
                             "\"%s\" which is the oldest possible time",
                             backup.name,
                             max_age.isoformat())
                expired_backups.append(backup)
        return expired_backups

    def _get_latest_backup(self):
        """
        Returns the latest/youngest backup, or None if there is none.
        """
        if len(self.backups) == 0:
            return None
        if len(self.backups) == 1:
            return self.backups[0]
        latest = self.backups[0]
        for backup in self.backups:
            if backup.date > latest.date:
                latest = backup
        return latest

    def _get_latest_backup_of_interval(self, interval):
        """
        Returns the latest/youngest backup of the given interval, or None if
        there is none.
        :param interval: The name of the interval to search for.
        :type interval: string
        """
        if len(self.backups) == 0:
            return None
        latest = None
        for backup in self.backups:
            if latest is None and backup.interval_name == interval:
                latest = backup
            elif latest is not None:
                if (backup.interval_name == interval and
                        backup.date > latest.date):
                    latest = backup
        return latest


class BackupParameters(object):

    def __init__(self, link_ref, rsync_cmd, rsync_args, rsync_filter,
                 rsync_logfile_options):
        self.link_ref = link_ref
        self.rsync_cmd = rsync_cmd
        self.rsync_args = rsync_args
        self.rsync_filter = rsync_filter
        self.rsync_logfile_options = rsync_logfile_options


class BackupFolder(object):

    def __init__(self, path):
        self.path = path
        self.meta_file = BackupMetaInfoFile(
            os.path.join(self.path, const.NAME_META_FILE))

    def read_meta_file(self):
        if not self.meta_file.exists():
            raise Exception("not a finished backup")
        logger.debug("Reading meta file of backup \"%s\".", self.path)
        try:
            self.meta_file.read()
        except IOError:
            raise

    def write_meta_file(self):
        self.meta_file.write()

    def set_meta_data(self, name, date, interval):
        self.meta_file.set_info(name, date, interval)

    def prepare(self):
        logger.debug("Preparing backup folder \"%s\".", self.path)
        os.mkdir(self.path)

    def is_finished(self):
        content = os.listdir(self.path)
        return (const.NAME_META_FILE in content and
                const.NAME_BACKUP_SUBFOLDER in content)

    @property
    def folder_name(self):
        return os.path.basename(self.path)

    @property
    def date(self):
        return self.meta_file.date

    @property
    def name(self):
        return self.meta_file.name

    @property
    def interval_name(self):
        return self.meta_file.interval

    @property
    def backup_path(self):
        return os.path.join(self.path, const.NAME_BACKUP_SUBFOLDER)

    @property
    def metafile_path(self):
        return os.path.join(self.path, const.NAME_META_FILE)


class BackupMetaInfoFile(object):

    def __init__(self, path):
        self.path = path
        self.name = None
        self.date = None
        self.interval = None

    def read(self):
        logger.debug("Reading meta file \"%s\".", self.path)
        try:
            lines = open(self.path).readlines()
        except IOError:
            raise
        logger.debug("Content: %s.", lines)

        self.name = lines[const.META_FILE_INDEX_NAME].strip()
        logger.debug("Name set to \"%s\".", self.name)
        self.date = datetime.datetime.strptime(
            lines[const.META_FILE_INDEX_DATE].strip(),
            const.META_FILE_DATE_FORMAT)
        logger.debug("Date set to \"%s\".", self.date.isoformat())
        self.interval = lines[const.META_FILE_INDEX_INTERVAL].strip()
        logger.debug("Interval set to \"%s\".", self.interval)

    def set_info(self, name, date, interval):
        self.name = name
        self.date = date
        self.interval = interval

    def write(self):
        logger.debug("Writing meta file \"%s\".", self.path)
        content = self._get_string()
        logger.debug("Content to write: \"%s\".", content)
        with open(self.path, 'w') as fd:
            fd.write(content)

    def _get_string(self):
        # yeah the following is not shitty at all
        content = [None] * const.META_FILE_LINES
        content[const.META_FILE_INDEX_NAME] = self.name
        content[const.META_FILE_INDEX_DATE] = self.date.strftime(
            const.META_FILE_DATE_FORMAT)
        content[const.META_FILE_INDEX_INTERVAL] = self.interval
        ret = "\n".join([str(f) for f in content]) + "\n"
        logger.debug("String assembled to write: \"%s\".", ret)
        return ret

    def exists(self):
        return os.path.exists(self.path)


class BackupInterval(object):

    def __init__(self, name, cronjob):
        self.name = name
        self.cronjob = cronjob
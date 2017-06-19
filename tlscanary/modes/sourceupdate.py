# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import csv
import datetime
import logging
import os
import sys
import zipfile

from basemode import BaseMode
from tlscanary.firefox_downloader import get_to_file
import tlscanary.sources_db as sdb


logger = logging.getLogger(__name__)


class SourceUpdateMode(BaseMode):
    """
    Mode to update the `top` host database from publicly available top sites data
    """

    name = "srcupdate"

    # There are various top sites databases that might be considered for querying here.
    # The other notable database is the notorious `Alexa Top 1M` which is available at
    # "http://s3.amazonaws.com/alexa-static/top-1m.csv.zip". It is based on usage data
    # gathered from the equally notorious Alexa browser toolbar, while the `Umbrella top 1M`
    # used below is DNS-based and its ranking is hence considered to be more representative.
    # `Umbrella` and `Alexa` use precisely the same format and their links are thus
    # interchangeable.
    # For future reference, there is also Ulfr's database at
    # "https://ulfr.io/f/top1m_has_tls_sorted.csv". It requires a different parser but
    # has the advantage of clustering hosts by shared certificates.

    top_sites_location = "http://s3-us-west-1.amazonaws.com/umbrella-static/top-1m.csv.zip"

    def __init__(self, args, module_dir, tmp_dir):
        super(SourceUpdateMode, self).__init__(args, module_dir, tmp_dir)
        self.start_time = None
        self.db = None
        self.sources = None
        self.app = None
        self.profile = None
        self.result = None

    def setup(self):
        global logger

        self.app = self.get_test_candidate(self.args.base)
        self.profile = self.make_profile("base_profile")

        tmp_zip_name = os.path.join(self.tmp_dir, "top.zip")
        logger.info("Fetching unfiltered top sites data from the `Umbrella Top 1M` online database")
        get_to_file(self.top_sites_location, tmp_zip_name)

        try:
            zipped = zipfile.ZipFile(tmp_zip_name)
            if len(zipped.filelist) != 1 or not zipped.filelist[0].orig_filename.lower().endswith(".csv"):
                logger.critical("Top sites zip file has unexpected content")
                sys.exit(5)
            tmp_csv_name = zipped.extract(zipped.filelist[0], self.tmp_dir)
        except zipfile.BadZipfile:
            logger.critical("Error opening top sites zip archive")
            sys.exit(5)

        self.db = sdb.SourcesDB(self.args)
        is_default = self.args.source == self.db.default
        self.sources = sdb.Sources(self.args.source, is_default)

        with open(tmp_csv_name) as f:
            cr = csv.DictReader(f, fieldnames=["rank", "hostname"])
            self.sources.rows = [row for row in cr]

        # A mild sanity check to see whether the downloaded data is valid.
        if len(self.sources) < 900000:
            logger.warning("Top sites is surprisingly small, just %d hosts" % len(self.sources))
        if self.sources.rows[0] != {"hostname": "google.com", "rank": "1"}:
            logger.warning("Top sites data looks weird. First line: `%s`" % self.sources.rows[0])

    def run(self):
        """
        Perform the filter run. The objective is to filter out permanent errors so
        we don't waste time on them during regular test runs.

        The concept is:
        Run top sites in chunks through Firefox and re-test all error URLs from that
        chunk a number of times to weed out spurious network errors. Stop the process
        once the required number of working hosts is collected.
        """
        global logger

        self.start_time = datetime.datetime.now()

        limit = 500000
        if self.args.limit is not None:
            limit = self.args.limit

        logger.info("There are %d hosts in the unfiltered host set" % len(self.sources))
        logger.info("Compiling set of %d working hosts for `%s` database update" % (limit, self.sources.handle))
        working_set = set()

        # Chop unfiltered sources data into chunks and iterate over each
        chunk_size = max(int(limit / 20), 1000)
        # TODO: Remove this log line once progress reporting is done properly
        logger.warning("Progress is reported per chunk of %d hosts, not overall" % chunk_size)

        for chunk_start in xrange(0, len(self.sources), chunk_size):

            hosts_to_go = max(0, limit - len(working_set))
            # Check if we're done
            if hosts_to_go == 0:
                break
            logger.info("%d hosts to go to complete the working set" % hosts_to_go)

            chunk_end = chunk_start + chunk_size
            # Shrink chunk if it contains way more hosts than required to complete the working set
            if chunk_size > hosts_to_go * 2:
                # CAVE: This assumes that this is the last chunk we require. The downsized chunk
                # is still 50% larger than required to complete the set to compensate for broken
                # hosts. If the error rate in the chunk is greater than 50%, another chunk will be
                # consumed, resulting in a gap of untested hosts between the end of this downsized
                # chunk and the beginning of the next. Not too bad, but important to be aware of.
                chunk_end = chunk_start + hosts_to_go * 2
            # Check if we're running out of data for completing the set
            if chunk_end > len(self.sources):
                chunk_end = len(self.sources)

            # Run chunk through multiple passes of Firefox, leaving only persistent errors in the
            # error set.
            logger.info("Processing chunk of %d hosts from the unfiltered set (#%d to #%d)"
                        % (chunk_end - chunk_start, chunk_start, chunk_end - 1))
            pass_chunk = self.sources.as_set(start=chunk_start, end=chunk_end)
            pass_errors = pass_chunk
            for _ in xrange(self.args.scans):
                pass_errors = self.run_test(self.app, pass_errors, profile=self.profile, get_info=False,
                                            get_certs=False, progress=True, return_only_errors=True)
                if len(pass_errors) == 0:
                    break

            logger.info("Error rate in chunk was %.1f%%"
                        % (100.0 * float(len(pass_errors)) / float(chunk_end - chunk_start)))

            # Add all non-errors to the working set
            working_set.update(pass_chunk.difference(pass_errors))

        final_src = sdb.Sources(self.sources.handle, is_default=self.sources.is_default)
        final_src.from_set(working_set)
        final_src.sort()
        final_src.trim(limit)

        if len(final_src) < limit:
            logger.warning("Ran out of hosts to complete the working set")

        self.result = final_src

    def report(self):
        # There is no actual report for this mode, just write out the database
        logger.info("Collected %d working hosts for the updated test set" % len(self.result))
        logger.info("Writing updated `%s` host database" % self.result.handle)
        self.db.write(self.result)

    def teardown(self):
        # Free some memory
        self.db = None
        self.sources = None
        self.app = None
        self.profile = None
        self.result = None
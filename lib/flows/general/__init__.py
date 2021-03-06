#!/usr/bin/env python
"""General purpose flows."""


# pylint: disable=unused-import
# These imports populate the Flow registry
from grr.lib.flows.general import administrative
from grr.lib.flows.general import aff4_notifiers
from grr.lib.flows.general import audit

# Disable these flows since they have no tests and are broken.
# from grr.lib.flows.general import automation
from grr.lib.flows.general import collectors
from grr.lib.flows.general import discovery

from grr.lib.flows.general import fetch_files
from grr.lib.flows.general import file_finder
from grr.lib.flows.general import filesystem
from grr.lib.flows.general import filetypes
from grr.lib.flows.general import find
from grr.lib.flows.general import fingerprint
from grr.lib.flows.general import grep

from grr.lib.flows.general import memory
from grr.lib.flows.general import network
from grr.lib.flows.general import processes
from grr.lib.flows.general import registry
from grr.lib.flows.general import screenshot
from grr.lib.flows.general import timelines
from grr.lib.flows.general import transfer
from grr.lib.flows.general import webhistory
from grr.lib.flows.general import webplugins
from grr.lib.flows.general import windows_vsc

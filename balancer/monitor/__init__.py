#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#

"""
monitor package – public API for all system monitoring components.

Importing from this package is preferred over importing individual sub-modules
directly, because it decouples callers from the internal file layout and makes
it easy to relocate or rename implementation files in the future.

Usage::

    from monitor import (
        PSIMonitor,
        ResourceMonitor,
        CgroupMonitor,
        PressureAnalyzer,
        NetworkMonitor,
        WindowDiffHistory,
        AppIntercept,
    )
"""

from .psi import PSIMonitor
from .res_monitor import ResourceMonitor
from .cgroup import CgroupMonitor
from .pressure import PressureAnalyzer
from .network import NetworkMonitor, WindowDiffHistory
from .appIntercept import AppIntercept

__all__ = [
    "PSIMonitor",
    "ResourceMonitor",
    "CgroupMonitor",
    "PressureAnalyzer",
    "NetworkMonitor",
    "WindowDiffHistory",
    "AppIntercept",
]
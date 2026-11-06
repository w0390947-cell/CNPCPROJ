"""Compose example identity with workflow observations at the web adapter boundary."""
from typing import Literal
from pydantic import RootModel
from oilfield_energy.modules.studies.contracts_patent2 import Patent2Inputs
from oilfield_energy.workflows.cluster_execution.contracts_live import (
    DisplayEvent, ClusterInitial, ClusterCycle, ClusterWindow,
)


class ClusterStudy(DisplayEvent):
    kind: Literal["cluster_study"]
    inputs: Patent2Inputs


class ClusterLiveEvent(RootModel[ClusterStudy | ClusterInitial | ClusterCycle | ClusterWindow]):
    pass

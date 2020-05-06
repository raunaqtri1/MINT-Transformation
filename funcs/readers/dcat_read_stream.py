#!/usr/bin/python
# -*- coding: utf-8 -*-

from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Union, Generator

from dtran.argtype import ArgType
from dtran.ifunc import IFunc, IFuncType


class DcatReadStream(IFunc):
    id = "dcat_read_stream"
    description = """ An entry point in the pipeline.
    Fetches a dataset and its metadata from the MINT Data-Catalog.
    """
    func_type = IFuncType.READER
    friendly_name: str = "Data Catalog Stream Reader"
    inputs = {
        "start_time": ArgType.DateTime,
        "end_time": ArgType.DateTime,
    }
    outputs = {
        "start_time": ArgType.DateTime,
        "end_time": ArgType.DateTime
    }
    example = {
        "start_time": "2020-03-02T12:30:55",
        "end_time": "2020-03-02T12:30:55",
    }

    def __init__(self, start_time: datetime, end_time: datetime):
        self.start_time = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        self.end_time = end_time.replace(hour=0, minute=0, second=0, microsecond=0)

    def exec(self) -> Union[dict, Generator[dict, None, None]]:
        start_time = self.start_time
        while start_time < self.end_time:
            end_time = start_time + relativedelta(days=1)
            yield {"start_time": start_time, "end_time": end_time}
            start_time = end_time

    def validate(self) -> bool:
        return True

from __future__ import annotations

import enum
import importlib
import sys
import types
from pathlib import Path


WHEEL_BOT_DIR = Path(__file__).resolve().parents[1]
if str(WHEEL_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(WHEEL_BOT_DIR))


def fresh_import(name: str):
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def install_apscheduler_stubs() -> None:
    apscheduler = types.ModuleType("apscheduler")
    schedulers = types.ModuleType("apscheduler.schedulers")
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")

    class BlockingScheduler:
        def __init__(self, timezone=None):
            self.timezone = timezone
            self.jobs = []
            self.started = False

        def add_job(self, func, trigger, kwargs=None, id=None, name=None, **opts):
            job = types.SimpleNamespace(
                func=func,
                trigger=trigger,
                kwargs=kwargs or {},
                id=id,
                name=name,
                opts=opts,
                next_run_time="stub-next-run",
            )
            self.jobs.append(job)
            return job

        def get_jobs(self):
            return self.jobs

        def start(self):
            self.started = True

        def shutdown(self, wait=False):
            self.started = False
            self.wait = wait

    class CronTrigger:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    blocking.BlockingScheduler = BlockingScheduler
    cron.CronTrigger = CronTrigger

    sys.modules.update(
        {
            "apscheduler": apscheduler,
            "apscheduler.schedulers": schedulers,
            "apscheduler.schedulers.blocking": blocking,
            "apscheduler.triggers": triggers,
            "apscheduler.triggers.cron": cron,
        }
    )


def install_alpaca_stubs(
    option_quotes: dict | None = None,
    option_snapshots: dict | None = None,
) -> None:
    alpaca = types.ModuleType("alpaca")
    trading = types.ModuleType("alpaca.trading")
    enums = types.ModuleType("alpaca.trading.enums")
    requests = types.ModuleType("alpaca.trading.requests")
    client_mod = types.ModuleType("alpaca.trading.client")
    data = types.ModuleType("alpaca.data")
    historical = types.ModuleType("alpaca.data.historical")
    option_mod = types.ModuleType("alpaca.data.historical.option")
    data_requests = types.ModuleType("alpaca.data.requests")

    class OrderSide(str, enum.Enum):
        BUY = "buy"
        SELL = "sell"

    class TimeInForce(str, enum.Enum):
        DAY = "day"

    class OrderClass(str, enum.Enum):
        MLEG = "mleg"

    class ContractType(str, enum.Enum):
        CALL = "call"
        PUT = "put"

    class PositionIntent(str, enum.Enum):
        BUY_TO_OPEN = "buy_to_open"
        BUY_TO_CLOSE = "buy_to_close"
        SELL_TO_OPEN = "sell_to_open"
        SELL_TO_CLOSE = "sell_to_close"

    class LimitOrderRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class OptionLegRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class GetOptionContractsRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class TradingClient:
        def __init__(self, key, secret, paper=True):
            self.key = key
            self.secret = secret
            self.paper = paper

    class OptionLatestQuoteRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class OptionSnapshotRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            for key, value in kwargs.items():
                setattr(self, key, value)

    class OptionHistoricalDataClient:
        def __init__(self, api_key, secret_key):
            self.api_key = api_key
            self.secret_key = secret_key

        def get_option_latest_quote(self, request_params):
            return option_quotes or {}

        def get_option_snapshot(self, request_params):
            return option_snapshots or {}

    enums.OrderSide = OrderSide
    enums.TimeInForce = TimeInForce
    enums.OrderClass = OrderClass
    enums.ContractType = ContractType
    enums.PositionIntent = PositionIntent
    requests.LimitOrderRequest = LimitOrderRequest
    requests.OptionLegRequest = OptionLegRequest
    requests.GetOptionContractsRequest = GetOptionContractsRequest
    client_mod.TradingClient = TradingClient
    option_mod.OptionHistoricalDataClient = OptionHistoricalDataClient
    data_requests.OptionLatestQuoteRequest = OptionLatestQuoteRequest
    data_requests.OptionSnapshotRequest = OptionSnapshotRequest

    sys.modules.update(
        {
            "alpaca": alpaca,
            "alpaca.trading": trading,
            "alpaca.trading.enums": enums,
            "alpaca.trading.requests": requests,
            "alpaca.trading.client": client_mod,
            "alpaca.data": data,
            "alpaca.data.historical": historical,
            "alpaca.data.historical.option": option_mod,
            "alpaca.data.requests": data_requests,
        }
    )


def install_langgraph_stubs() -> None:
    lc_messages = types.ModuleType("langchain_core.messages")
    langchain_core = types.ModuleType("langchain_core")
    langchain_openai = types.ModuleType("langchain_openai")
    langgraph = types.ModuleType("langgraph")
    graph_mod = types.ModuleType("langgraph.graph")
    graph_message = types.ModuleType("langgraph.graph.message")
    checkpoint = types.ModuleType("langgraph.checkpoint")
    checkpoint_sqlite = types.ModuleType("langgraph.checkpoint.sqlite")
    checkpoint_memory = types.ModuleType("langgraph.checkpoint.memory")

    class BaseMessage:
        def __init__(self, content=""):
            self.content = content

    class AIMessage(BaseMessage):
        pass

    class HumanMessage(BaseMessage):
        pass

    class SystemMessage(BaseMessage):
        pass

    class ChatOpenAI:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class StateGraph:
        def __init__(self, state_type):
            self.state_type = state_type
            self.nodes = {}
            self.edges = []

        def add_node(self, name, func):
            self.nodes[name] = func

        def add_edge(self, start, end):
            self.edges.append((start, end))

        def add_conditional_edges(self, start, route, mapping):
            self.edges.append((start, route, mapping))

        def compile(self, checkpointer=None):
            class App:
                def invoke(self, initial, config=None):
                    return dict(initial)

            return App()

    class SqliteSaver:
        def __init__(self, conn):
            self.conn = conn

    class MemorySaver:
        pass

    def add_messages(left, right):
        return list(left or []) + list(right or [])

    lc_messages.AIMessage = AIMessage
    lc_messages.BaseMessage = BaseMessage
    lc_messages.HumanMessage = HumanMessage
    lc_messages.SystemMessage = SystemMessage
    langchain_openai.ChatOpenAI = ChatOpenAI
    graph_mod.END = "__end__"
    graph_mod.START = "__start__"
    graph_mod.StateGraph = StateGraph
    graph_message.add_messages = add_messages
    checkpoint_sqlite.SqliteSaver = SqliteSaver
    checkpoint_memory.MemorySaver = MemorySaver

    sys.modules.update(
        {
            "langchain_core": langchain_core,
            "langchain_core.messages": lc_messages,
            "langchain_openai": langchain_openai,
            "langgraph": langgraph,
            "langgraph.graph": graph_mod,
            "langgraph.graph.message": graph_message,
            "langgraph.checkpoint": checkpoint,
            "langgraph.checkpoint.sqlite": checkpoint_sqlite,
            "langgraph.checkpoint.memory": checkpoint_memory,
        }
    )

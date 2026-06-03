"""Tools the boardroom agent calls.

The ADK turns each function's signature + docstring into the tool schema
Gemini sees. Docstrings are the contract — keep them precise.

Tools group by responsibility:
  Portfolio / state (MongoDB MCP):
    - get_portfolio: read user holdings + cash + target allocation
    - log_decision: append a user-confirmed action to decision_diary
    - record_score_block: persist a director's per-topic score
    - record_argument: persist a debate rebuttal
    - record_verdict: persist the Chairman's final synthesis
  Market intelligence (real APIs):
    - get_quote: Alpha Vantage last price
    - get_analyst_consensus: hybrid Finnhub + yfinance
    - get_peer_comp: peer median PEG / PE
    - get_macro_snapshot: FRED yield curve + CPI + Fed funds
    - search_pattern_library: cross-call pattern lookup
    - search_track_records: director historical accuracy lookup
  Execution:
    - draft_paper_trade: persist a draft order awaiting user approval
    - execute_paper_trade: send the approved order to Alpaca paper
  Audio:
    - start_listening: register a new audio source
    - end_listening: mark source ended and trigger final synthesis
"""

# All tools are async functions registered with the ADK LlmAgent.
# We export ALL_TOOLS as the canonical registration list so root_agent.py
# can keep its definition tight.

from .portfolio import (
    get_portfolio,
    log_decision,
    record_score_block,
    record_argument,
    record_verdict,
)
from .market import (
    get_quote,
    get_analyst_consensus,
    get_peer_comp,
    get_macro_snapshot,
)
from .memory import (
    search_pattern_library,
    search_track_records,
)
from .execution import (
    draft_paper_trade,
    execute_paper_trade,
)
from .audio_session import (
    start_listening,
    end_listening,
)
from .synthesis import synthesize_verdict

ALL_TOOLS = [
    # Portfolio + state
    get_portfolio,
    log_decision,
    record_score_block,
    record_argument,
    record_verdict,
    # Market intelligence
    get_quote,
    get_analyst_consensus,
    get_peer_comp,
    get_macro_snapshot,
    # Long-term memory
    search_pattern_library,
    search_track_records,
    # Execution
    draft_paper_trade,
    execute_paper_trade,
    # Audio session
    start_listening,
    end_listening,
    # Synthesis
    synthesize_verdict,
]

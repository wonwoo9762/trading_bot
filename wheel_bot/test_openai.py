"""Test that OpenAI API key is set and the API is reachable."""
from langchain_openai import ChatOpenAI

from config import require_openai_key


def test_openai_connection() -> str:
    """Call OpenAI with a simple prompt; return the reply or raise."""
    require_openai_key()
    llm = ChatOpenAI(model="gpt-5-mini", temperature=0)
    response = llm.invoke("Reply with exactly: OpenAI connection OK.")
    return response.content


if __name__ == "__main__":
    try:
        reply = test_openai_connection()
        print("OpenAI response:", reply)
        print("Connection test passed.")
    except Exception as e:
        print("Connection test failed:", e)
        raise

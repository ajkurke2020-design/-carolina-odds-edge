import requests

OLLAMA_URL = "http://localhost:11434/api/generate"


def analyze_play_with_ollama(play_summary: dict, model: str = "qwen2.5:3b") -> str:
    prompt = f"""
    You are a professional quantitative sports betting analyst. Review this +EV betting opportunity:
    - Sport/Matchup: {play_summary.get('matchup')} ({play_summary.get('sport')})
    - Market & Selection: {play_summary.get('market')} - {play_summary.get('selection')}
    - Soft Bookmaker & Odds: {play_summary.get('sportsbook')} at {play_summary.get('OddsAmerican', play_summary.get('odds_american'))}
    - Calculated Expected Value (+EV): {play_summary.get('EV_Pct', play_summary.get('expected_ev_pct'))}%

    Provide a concise, 3-sentence sanity check explaining:
    1. Where the probabilistic edge comes from.
    2. Whether this line looks like a normal market variance or a risky stale line.
    """

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 250},
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=30)
        if response.status_code == 200:
            return response.json().get("response", "No response generated.")
        return f"Error connecting to Ollama: Status code {response.status_code}"
    except requests.exceptions.ConnectionError:
        return "Could not connect to Ollama. Make sure Ollama is running locally (`ollama serve`)."
    except Exception as exc:
        return f"Error: {str(exc)}"

#!/usr/bin/env python3
"""
Crypto Market Intelligence Agent - Haiku 4.5 Runner
Executes the agent with claude-haiku-4-5-20251001 for cost-optimized daily runs.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
import anthropic

def load_claude_instructions():
    """Load the CLAUDE.md instructions as the system prompt."""
    claude_md_path = Path(__file__).parent / "CLAUDE.md"
    with open(claude_md_path, 'r') as f:
        return f.read()

def load_state():
    """Load current state from state.json."""
    state_path = Path(__file__).parent / "state.json"
    if state_path.exists():
        with open(state_path, 'r') as f:
            return json.load(f)
    return {}

def load_env():
    """Load environment variables from .env file."""
    env_path = Path(__file__).parent / ".env"
    env_vars = {}
    if env_path.exists():
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if '=' in line:
                        key, value = line.split('=', 1)
                        env_vars[key] = value
    return env_vars

def get_api_key():
    """Get ANTHROPIC_API_KEY from environment, .env file, or raise error."""
    # Try environment variable first
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key

    # Try .env file
    env_vars = load_env()
    api_key = env_vars.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key

    # Not found
    raise ValueError(
        "ANTHROPIC_API_KEY not found. Please set it:\n"
        "  export ANTHROPIC_API_KEY='your-key-here'\n"
        "  or add it to /home/user/Agents/.env file"
    )

def run_agent():
    """Execute the crypto market intelligence agent with Haiku 4.5."""

    print(f"[{datetime.utcnow().isoformat()}] Starting Crypto Market Intelligence Agent (Haiku 4.5)...")

    try:
        api_key = get_api_key()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Load instructions and state
    system_prompt = load_claude_instructions()
    current_state = load_state()
    env_vars = load_env()

    # Build user prompt with current state
    user_prompt = f"""Today is {datetime.utcnow().strftime('%Y-%m-%d')}.

Current state from last run:
{json.dumps(current_state, indent=2)}

Execute all 11 steps of the market intelligence analysis. Return:
1. Updated state.json structure with all fields populated
2. Daily report formatted as shown in STEP 9
3. Clear summary of actions taken

Ensure the output is valid JSON for state updates and includes the formatted email report."""

    # Initialize Anthropic client with Haiku model
    client = anthropic.Anthropic(api_key=api_key)

    print(f"[{datetime.utcnow().isoformat()}] Calling Claude Haiku 4.5 API...")

    # Call Claude with Haiku model
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": user_prompt
            }
        ]
    )

    response_text = message.content[0].text

    print(f"[{datetime.utcnow().isoformat()}] Agent analysis complete.")
    print(f"[{datetime.utcnow().isoformat()}] Tokens used - Input: {message.usage.input_tokens}, Output: {message.usage.output_tokens}")

    # Extract and save state.json from response
    # The response should contain a JSON block with the updated state
    try:
        # Look for JSON in the response
        json_start = response_text.find('{')
        json_end = response_text.rfind('}') + 1

        if json_start >= 0 and json_end > json_start:
            json_str = response_text[json_start:json_end]
            updated_state = json.loads(json_str)

            state_path = Path(__file__).parent / "state.json"
            with open(state_path, 'w') as f:
                json.dump(updated_state, f, indent=2)

            print(f"[{datetime.utcnow().isoformat()}] State updated: {state_path}")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[{datetime.utcnow().isoformat()}] Warning: Could not extract state JSON from response: {e}")

    # Log to report.log
    try:
        report_log_path = Path(__file__).parent / "report.log"
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

        # Extract macro bias and setup count if available
        macro_bias = "N/A"
        setup_count = "N/A"

        if macro_bias in response_text:
            for bias in ["BULLISH", "BEARISH", "NEUTRAL", "BIFURCATED"]:
                if bias in response_text:
                    macro_bias = bias
                    break

        log_entry = f"{timestamp} UTC | {macro_bias} | Agent run complete | Haiku 4.5"

        with open(report_log_path, 'a') as f:
            f.write(log_entry + "\n")

        print(f"[{datetime.utcnow().isoformat()}] Log updated: {report_log_path}")
    except Exception as e:
        print(f"[{datetime.utcnow().isoformat()}] Warning: Could not update report.log: {e}")

    # Save daily report to file
    try:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        report_path = Path(__file__).parent / f"daily_report_{date_str}.txt"
        with open(report_path, 'w') as f:
            f.write(response_text)
        print(f"[{datetime.utcnow().isoformat()}] Report saved: {report_path}")
    except Exception as e:
        print(f"[{datetime.utcnow().isoformat()}] Warning: Could not save report: {e}")

    # Display the full response
    print("\n" + "="*80)
    print("AGENT RESPONSE:")
    print("="*80)
    print(response_text)
    print("="*80)

    return 0

if __name__ == "__main__":
    sys.exit(run_agent())

import os
import re
from datetime import datetime
import discord
from discord.ext import commands
import requests
import urllib.parse
from openai import OpenAI
import aiohttp

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="$._dummy_", intents=intents)

GOOGLE_FACT_CHECK_API_KEY = os.getenv("GOOGLE_KEY")
# If integrating GreenPT text completions later:
GREENPT_API_KEY = os.getenv("GREENPT_KEY") 

green_client = OpenAI(base_url="https://api.greenpt.ai/v1", api_key=GREENPT_API_KEY)

MAX_REPLY_LENGTH = 1900

SYSTEM_PROMPT = (
    "You are Tammy, a precise fact-checking bot. "
    "CRITICAL INSTRUCTION: Never output your reasoning, internal thoughts, source breakdowns, lists, or analysis. "
    "Output ONLY the final requested fact-check structure."
)

URL_PATTERN = re.compile(r"https?://[^\s)\]>]+")


def format_month_year(raw_date):
    """Format an ISO 8601 / YYYY-MM-DD date as 'Month Year'."""
    raw_date = str(raw_date or "").strip()
    if not raw_date:
        return "Unknown Date"
    try:
        return datetime.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%B %Y")
    except ValueError:
        pass
    try:
        return datetime.strptime(raw_date[:10], "%Y-%m-%d").strftime("%B %Y")
    except ValueError:
        pass
    match = re.match(r"^(\d{4})-(\d{2})", raw_date)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), 1).strftime("%B %Y")
        except ValueError:
            return match.group(0)
    return "Unknown Date"


def normalize_reply(text):
    """Force a model reply into a single plain-text line ending in '(URL)'."""
    text = text or ""

    # Remove markdown bold markers
    text = text.replace("**", "").replace("__", "")

    # Keep the first URL found anywhere in the reply
    url_match = URL_PATTERN.search(text)
    url = url_match.group(0).rstrip(".,;:!?'\"") if url_match else None

    # Drop "Source" lines and lines carrying a "Source:" field
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"(?i)^source", stripped) or re.search(r"(?i)source\s*:", stripped):
            continue
        lines.append(stripped)
    body = " ".join(lines)

    # Strip markdown links and raw URLs from the body, then tidy up leftovers
    body = re.sub(r"\[([^\]]*)\]\(\s*https?://[^)\s]*\s*\)", r"\1", body)
    body = URL_PATTERN.sub("", body)
    body = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", body)
    body = re.sub(r"\s+", " ", body).strip()

    suffix = f" ({url})" if url else ""
    limit = MAX_REPLY_LENGTH - len(suffix)
    if len(body) > limit:
        body = body[:max(limit, 0)].rstrip()
    return f"{body}{suffix}".strip()[:MAX_REPLY_LENGTH]

async def fetch_tavily_search(query: str) -> str:
    """Queries Tavily API for real-time news snippets, real dates, and live source URLs."""
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        return ""
    
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": tavily_key,
        "query": query,
        "search_depth": "basic",
        "topic": "news",
        "max_results": 5,
        "include_answer": False
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    
                    # Flatten results into a neat context block for GLM to read
                    context_lines = []
                    for res in results:
                        context_lines.append(
                            f"Source URL: {res.get('url')}\n"
                            f"Real Content/Date Info: {res.get('content')}"
                        )
                    return "\n\n".join(context_lines)
    except Exception as e:
        print(f"Tavily connection error: {e}")
    return ""

@bot.event
async def on_ready():
    print(f"Tammy is online as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content = message.content.lower()
    trigger_phrases = [
        "tammy check this", "tammy check", "tammy is that true", "tammy is it true", "tammy verify", "tammy fact check if", "tammy factcheck if",
        "tammy fact check this", "tammy is it true that", "tammy is it true that the", "tammy factcheck", "tammmy factcheck this",
        "tammy verify this", "tammy can you verify", "tammy is this true", "tammy fact check", "tammy fact check this",
    ]

    import re
    clean_content = re.sub(r'[^\w\s]', '', content).strip()
    matched_phrase = next((phrase for phrase in trigger_phrases if clean_content.startswith(phrase)), None)
    triggered = matched_phrase is not None

    if bot.user in message.mentions or triggered or "tammy" in content:
        try:
            await message.add_reaction("💛")
        except Exception as e:
            print(f"Failed to add reaction: {e}")

    if triggered:
        clean_content = re.sub(r'[^\w\s]', '', content).strip()
        matched_phrase = next((p for p in trigger_phrases if clean_content.startswith(p)), "")
        
        words = message.content.split()
        phrase_word_count = len(matched_phrase.split())
        claim = " ".join(words[phrase_word_count:]).strip()
        claim = re.sub(r'^[\s"\'?!.,]+|[\s"\'?!.,]+$', '', claim)

        if message.reference:
            try:
                replied_msg = await message.channel.fetch_message(message.reference.message_id)
                ref_text = re.sub(r'https?://[^\s]+', '', replied_msg.content)
                ref_text = ref_text.replace('**', '').replace('__', '').strip()
                ref_text = re.sub(r'^[\s"\'?!.,]+|[\s"\'?!.,]+$', '', ref_text)
                claim = f"{ref_text} {claim}".strip() if claim else ref_text
            except Exception:
                pass

        if not claim:
            await message.channel.send("Please provide a claim for me to check!")
            return
            
        async with message.channel.typing():
            # Google Fact Check API Endpoint URL
            url = f"https://factchecktools.googleapis.com/v1alpha1/claims:search?query={urllib.parse.quote(claim)}&key={GOOGLE_FACT_CHECK_API_KEY}"
        
        try:
            response = requests.get(url).json()
        except Exception:
            response = {}

        google_context = ""
        try:
            if response and "claims" in response and len(response["claims"]) > 0:
                first_claim = response["claims"][0]
                claim_text = first_claim.get("text", "Unknown Claim")
                review = first_claim.get("claimReview", [{}])[0]
                pub_data = review.get("publisher", {})
                publisher = pub_data.get("name", "Unknown Source") if isinstance(pub_data, dict) else "Unknown Source"
                review_url = review.get("url", "No Link Provided")
                verdict = review.get("textualRating", "No clear verdict")
                raw_date = review.get("reviewDate", first_claim.get("claimDate", "Unknown Date"))
                clean_date = format_month_year(raw_date)
            
                google_context = (
                    f"Google Fact Check Database Match:\n"
                    f"Claim: {claim_text}\n"
                    f"Publisher: {publisher}\n"
                    f"Existing Verdict: {verdict}\n"
                    f"Published Date: {clean_date}\n"
                    f"Source URL: {review_url}\n\n"
                )
        except Exception as e:
            print(f"Google Fact Check parse error (ignoring): {e}")
            google_context = ""
        
        try:
            STREAMLINED_SYSTEM_PROMPT = (
                "You are Tammy, a precise fact-checking bot. "
                "Output ONLY the requested fact-check structure. "
                "Never include conversational filler, meta-commentary, reasoning steps, or sentence counts."
            )
            
            completion = green_client.chat.completions.create(
                model="glm-5.3", 
                messages=[
                    {"role": "system", "content": STREAMLINED_SYSTEM_PROMPT},
                    {
                        "role": "user", 
                        "content": (
                            f"Context Data:\n{google_context}{await fetch_tavily_search(claim)}\n\n"
                            f"Claim to verify: {claim}\n\n"
                            "Strictly follow this exact format and nothing else:\n"
                            "This is **[True/False/Partially True/Subjective/Open-ended/Misleading/Unverified]**. Dated and published in [Date]. [One sentence summary of the verification].\n\n"
                            "[One sentence interesting context fact].\n\n"
                            "([Real Source URL])"
                           "CRITICAL DATE RULE: Replace [date] with the actual publication month and year found in the context data (e.g. October, 2026. Look closely at URLs, content snippets, or article text for dates). If no explicit date is found in the text or URL, replace [date] with ‘recent registries’."
                            "CRITICAL LENGTH RULE: Keep both sentences brief and concise so they never get cut off."
                            "CRITICAL ANSWER RULE: If the input is a definition or open-ended question, use 'Open-Ended' as the verdict and define it concisely. \n"
                        )
                    }
                ],
                temperature=0.0,
                max_tokens=650
            )
            
            raw = completion.choices[0].message
            text = getattr(raw, 'content', '') or getattr(raw, 'reasoning_content', '') or ''
            text = text.strip()

            # Clean up potential LLM conversational leakage
            if "This is" in text:
                text = "This is " + text.split("This is")[-1].strip()
            elif "Hm, I couldn't" not in text:
                 # Fallback safety if it missed the formatting entirely
                 text = f"Hm, I couldn't find anything related to '{claim}'."
            
            if not text:
                raise ValueError("Empty reply from GreenPT")
                
            await message.channel.send(text[:1900])
            
        except Exception as e:
            print(f"GreenPT error: {e}")
            if "503" in str(e):
                await message.channel.send("⚠️ Uh oh. My technoturtle servers are overheating! Try your fact-check again in a moment.")
            else:
                await message.channel.send(f"⚠️ Green AI error: {e}"[:1900])

    await bot.process_commands(message)
    
# Run bot using Railway's environment variable
bot.run(os.getenv("DISCORD_TOKEN"))

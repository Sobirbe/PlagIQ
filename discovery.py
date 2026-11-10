"""Bounded academic metadata/abstract discovery through OpenAlex.

Never fetches arbitrary URLs or publisher PDFs. OpenAlex is queried with a
short excerpt of the manuscript; the service only compares returned abstracts.
"""
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

API = "https://api.openalex.org/works"

def reconstruct_abstract(inverted):
    """Reassemble an OpenAlex inverted abstract index into readable text."""
    if not isinstance(inverted, dict):
        return ""
    positions = {}
    for term, indexes in inverted.items():
        if not isinstance(term, str) or not isinstance(indexes, list):
            continue
        for p in indexes:
            if isinstance(p, int) and 0 <= p < 5000:
                positions[p] = term
    return " ".join(positions[x] for x in sorted(positions))

def suggest_queries(text):
    """Send short excerpts only; not an entire unpublished manuscript."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if 35 <= len(line) <= 220 and len(line.split()) >= 6]
    if not lines:
        lines = [re.sub(r"\s+", " ", text).strip()]
    query = " ".join(lines[0].split()[:14])[:220]
    return [query] if query.strip() else []

def discover(text, max_results=8):
    query = suggest_queries(text)
    if not query:
        return {"status":"no_query","sources":[],"queries":0,"message":"No search phrase could be extracted"}
    params = {"search":query[0], "per_page":str(min(max_results, 10)),
              "select":"id,display_name,doi,publication_year,abstract_inverted_index,primary_location"}
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    request = Request(API + "?" + urlencode(params),
                      headers={"User-Agent":"PlagIQ-research-pilot/0.2 (+academic metadata search)",
                               "Accept":"application/json"})
    try:
        with urlopen(request, timeout=9) as response:
            payload = json.load(response)
    except (TimeoutError, HTTPError, URLError, OSError, ValueError) as exc:
        return {"status":"unavailable","sources":[],"queries":1,
                "message":"OpenAlex unavailable or rate limited (" + type(exc).__name__ + ")"}
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        return {"status":"unavailable", "sources":[], "queries":1, "message":"Invalid OpenAlex response"}
    sources = []
    for work in payload.get("results", [])[:max_results]:
        if not isinstance(work, dict):
            continue
        title = str(work.get("display_name") or "Untitled")
        url = work.get("doi") or work.get("id") or ""
        if not isinstance(url,str) or not url.startswith("https://"):
            continue
        abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
        sources.append({"title":title[:300],"url":url,"year":work.get("publication_year"),
                        "abstract":abstract[:20000],"coverage":"abstract" if abstract else "metadata_only"})
    return {"status":"searched","sources":sources,"queries":1,
            "message":"OpenAlex metadata searched; only returned abstracts can be text-compared"}

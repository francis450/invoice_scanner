import re
import json
import time
import requests
import frappe
from difflib import SequenceMatcher


def _gemini_text(prompt, api_key, model):
	"""Single text-only Gemini call. Returns the raw response string."""
	url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
	payload = {
		"contents": [{"parts": [{"text": prompt}]}],
		"generationConfig": {"maxOutputTokens": 2000, "temperature": 0},
	}
	frappe.logger().debug(f"[gemini_text] model={model} prompt_length={len(prompt)}")
	resp = requests.post(url, json=payload, timeout=30)
	frappe.logger().debug(f"[gemini_text] response status={resp.status_code}")
	resp.raise_for_status()
	response_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
	frappe.logger().debug(f"[gemini_text] response_text_length={len(response_text)}")
	return response_text


# Trade/slang name → catalogue name pattern for domain-specific matching.
# Keys are upper-cased search terms, values are substrings that matching catalogue items must contain.
# When an extracted name matches a key, candidates are filtered/boosted to only those containing the pattern.
DOMAIN_ALIASES = {
	"SQUARE CABLES": "T/E",
	"SQUARE CABLE": "T/E",
	"FLAT CABLES": "T/E",
	"FLAT CABLE": "T/E",
}


def _apply_domain_aliases(extracted_name, catalogue):
	"""If extracted_name matches a domain alias, filter catalogue to items containing the pattern.
	Returns (filtered_catalogue, alias_info) where alias_info explains what matched."""
	name_upper = extracted_name.upper().strip()
	for alias_key, pattern in DOMAIN_ALIASES.items():
		if alias_key in name_upper:
			filtered = [i for i in catalogue if pattern in i.item_name.upper()]
			frappe.logger().debug(
				f"[domain_alias] '{extracted_name}' matched alias '{alias_key}' -> pattern '{pattern}', "
				f"filtered {len(filtered)}/{len(catalogue)} catalogue items"
			)
			if filtered:
				return filtered, f"Alias '{alias_key}' -> '{pattern}'"
	return catalogue, None


def _fuzzy_score(extracted_name, catalogue_name):
	"""Compute a fuzzy similarity score between an extracted item name and a catalogue item name."""
	a = extracted_name.upper().strip()
	b = catalogue_name.upper().strip()

	# Exact match
	if a == b:
		return 1.0

	# SequenceMatcher ratio
	seq_score = SequenceMatcher(None, a, b).ratio()

	# Token overlap: how many extracted tokens appear in the catalogue name
	a_tokens = set(re.findall(r"[A-Z0-9./]+", a))
	b_tokens = set(re.findall(r"[A-Z0-9./]+", b))
	if a_tokens:
		token_overlap = len(a_tokens & b_tokens) / len(a_tokens)
	else:
		token_overlap = 0.0

	# Substring check — boost if catalogue name contains extracted name or vice versa
	substr_boost = 0.0
	if a in b or b in a:
		substr_boost = 0.3

	return max(seq_score, token_overlap, seq_score + substr_boost * 0.5)


def _get_fuzzy_candidates(extracted_name, catalogue, top_n=8, min_score=0.25):
	"""Return the top-N catalogue items most similar to the extracted name."""
	scored = []
	for item in catalogue:
		score = _fuzzy_score(extracted_name, item.item_name)
		scored.append((score, item))
	scored.sort(key=lambda x: x[0], reverse=True)
	candidates = [(s, i) for s, i in scored[:top_n] if s >= min_score]
	return candidates


def _match_items_to_catalogue(extracted_items, api_key, model):
	"""
	Two-stage matching:
	  1. Local fuzzy pre-filter narrows 390 catalogue items down to ~8 candidates per extracted item.
	  2. Gemini picks the best match from the short candidate list.
	  3. Items with no good candidates (< min_score) are left unmatched.

	Returns (items, match_failed).
	"""
	catalogue = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["item_code", "item_name"],
		limit=500,
		order_by="item_name asc",
	)
	frappe.logger().debug(
		f"[match_items] catalogue_size={len(catalogue)} extracted_items={len(extracted_items)}"
	)
	if not catalogue:
		frappe.logger().debug("[match_items] catalogue empty, skipping match")
		return extracted_items, False

	all_match_failed = False

	for idx, item in enumerate(extracted_items):
		item_name = item.get("item_name", "")

		# Apply domain aliases to narrow the catalogue before fuzzy matching
		search_catalogue, alias_info = _apply_domain_aliases(item_name, catalogue)

		candidates = _get_fuzzy_candidates(item_name, search_catalogue, top_n=8, min_score=0.25)

		# If domain alias filtered too aggressively, fall back to full catalogue
		if not candidates and search_catalogue is not catalogue:
			frappe.logger().debug(
				f"[match_items] item {idx} '{item_name}': alias filter too narrow, falling back to full catalogue"
			)
			candidates = _get_fuzzy_candidates(item_name, catalogue, top_n=8, min_score=0.25)

		if not candidates:
			frappe.logger().debug(
				f"[match_items] item {idx} '{item_name}': no fuzzy candidates above threshold, skipping"
			)
			continue

		candidate_lines = "\n".join(
			f"  {i.item_code} | {i.item_name} (similarity: {score:.2f})" for score, i in candidates
		)
		frappe.logger().debug(f"[match_items] item {idx} '{item_name}' top candidates:\n{candidate_lines}")

		# If the best candidate is very high confidence, use it directly without Gemini
		best_score, best_item = candidates[0]
		if best_score >= 0.9:
			item["item_code"] = best_item.item_code
			item["item_name"] = best_item.item_name
			frappe.logger().debug(
				f"[match_items] item {idx} '{item_name}' -> {best_item.item_code} (direct fuzzy match, score={best_score:.2f})"
			)
			continue

		# Ask Gemini to pick from the short candidate list
		candidate_text = "\n".join(f"{i.item_code} | {i.item_name}" for _, i in candidates)
		alias_hint = ""
		if alias_info:
			alias_hint = f'\nNote: "{item_name}" is a trade name for items containing "{DOMAIN_ALIASES.get(item_name.upper().strip(), "")}" in their catalogue name. '
		prompt = (
			'Given this extracted invoice item: "' + item_name + '"\n\n'
			"Which of these catalogue items is the best match?" + alias_hint + "\n" + candidate_text + "\n\n"
			"Rules:\n"
			'- Return ONLY a JSON object: {"item_code": "<code>", "item_name": "<catalogue name>"}\n'
			'- If none are a reasonable match, return: {"item_code": null, "item_name": null}\n'
			"- When in doubt, prefer the most commonly sold variant."
		)

		try:
			response_text = _gemini_text(prompt, api_key, model)
			frappe.logger().debug(f"[match_items] item {idx} Gemini response:\n{response_text}")
			fence = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", response_text)
			if fence:
				response_text = fence.group(1)
			match_result = json.loads(response_text)
			if match_result.get("item_code"):
				item["item_code"] = match_result["item_code"]
				item["item_name"] = match_result.get("item_name", item_name)
				frappe.logger().debug(
					f"[match_items] item {idx} '{item_name}' -> {match_result['item_code']} (Gemini match)"
				)
			else:
				frappe.logger().debug(f"[match_items] item {idx} '{item_name}': Gemini said no match")
		except Exception:
			frappe.logger().debug(f"[match_items] item {idx} Gemini match failed:\n{frappe.get_traceback()}")
			all_match_failed = True

	return extracted_items, all_match_failed


@frappe.whitelist()
def scan_invoice(image_b64, media_type="image/jpeg"):
	"""
	Send an invoice image to Gemini vision and extract line items as structured JSON.

	Args:
	    image_b64 (str): Raw base64-encoded image data (no data URI prefix).
	    media_type (str): MIME type, e.g. "image/jpeg" or "image/png".

	Returns:
	    dict: {
	        "supplier": str | None,
	        "date": str | None,       # YYYY-MM-DD
	        "scan_log": str,          # Invoice Scan Log name
	        "items": [
	            {"item_name": str, "item_code": str | None, "qty": float, "rate": float, "uncertain": bool}
	        ]
	    }
	"""
	frappe.logger().debug(
		f"[scan_invoice] called with media_type={media_type} image_b64_length={len(image_b64)}"
	)

	api_key = frappe.conf.get("gemini_api_key")
	if not api_key:
		frappe.throw("Gemini API key not configured. Add 'gemini_api_key' to your site_config.json.")

	prompt = (
		"Extract all line items from this handwritten sales invoice. "
		"Return ONLY a JSON object — no markdown, no extra text — with this exact structure:\n"
		'{"supplier": "<name or null>", "date": "<YYYY-MM-DD or null>", '
		'"items": [{"item_name": "<name>", "qty": <number>, "rate": <number>, "uncertain": <true|false>}]}\n'
		"If a quantity or rate is illegible, use your best guess and set uncertain: true for that item. "
		"All monetary values should be numbers without currency symbols."
	)

	model = frappe.conf.get("gemini_model", "gemini-2.5-flash")
	max_tokens = 4000
	url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
	payload = {
		"contents": [
			{
				"parts": [
					{"inline_data": {"mime_type": media_type, "data": image_b64}},
					{"text": prompt},
				]
			}
		],
		"generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
	}

	frappe.logger().debug(
		f"[scan_invoice] model={model} maxOutputTokens={max_tokens} prompt_length={len(prompt)}"
	)

	t_start = time.time()

	resp = None
	for attempt in range(3):
		frappe.logger().debug(f"[scan_invoice] API attempt {attempt + 1}/3")
		try:
			resp = requests.post(url, json=payload, timeout=60)
			frappe.logger().debug(f"[scan_invoice] attempt {attempt + 1} status={resp.status_code}")
			if resp.status_code == 429:
				try:
					retry_after = float(resp.headers.get("Retry-After", 30 * (attempt + 1)))
				except (ValueError, TypeError):
					retry_after = 30 * (attempt + 1)
				frappe.logger().debug(f"[scan_invoice] rate limited, retrying after {retry_after}s")
				time.sleep(retry_after)
				continue
			resp.raise_for_status()
			break
		except requests.exceptions.RequestException as e:
			frappe.logger().debug(f"[scan_invoice] request exception on attempt {attempt + 1}: {e}")
			if attempt == 2:
				frappe.log_error(frappe.get_traceback(), "Gemini API request failed")
				frappe.throw(f"Gemini API request failed: {e}")
			time.sleep(10 * (attempt + 1))
	else:
		frappe.throw("Gemini API is rate-limiting this key. Please wait a moment and try again.")

	# Parse the Gemini response
	resp_json = resp.json()
	frappe.logger().debug(f"[scan_invoice] full response keys: {list(resp_json.keys())}")

	usage = resp_json.get("usageMetadata", {})
	frappe.logger().debug(
		f"[scan_invoice] usage: promptTokens={usage.get('promptTokenCount')} "
		f"candidatesTokens={usage.get('candidatesTokenCount')} "
		f"totalTokens={usage.get('totalTokenCount')}"
	)

	candidates = resp_json.get("candidates", [])
	if candidates:
		finish_reason = candidates[0].get("finishReason")
		frappe.logger().debug(f"[scan_invoice] finishReason={finish_reason}")

	response_text = resp_json["candidates"][0]["content"]["parts"][0]["text"].strip()
	frappe.logger().debug(f"[scan_invoice] raw response length={len(response_text)}")
	frappe.logger().debug(f"[scan_invoice] raw response:\n{response_text}")

	# Strip markdown code fences if Gemini wraps the JSON
	match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", response_text)
	if match:
		response_text = match.group(1)
		frappe.logger().debug(f"[scan_invoice] stripped markdown fence, cleaned length={len(response_text)}")
	else:
		frappe.logger().debug("[scan_invoice] no markdown fence detected in response")

	try:
		result = json.loads(response_text)
		frappe.logger().debug(f"[scan_invoice] JSON parse succeeded. keys={list(result.keys())}")
		frappe.logger().debug(f"[scan_invoice] supplier={result.get('supplier')} date={result.get('date')}")
		frappe.logger().debug(f"[scan_invoice] items count={len(result.get('items', []))}")
		for idx, item in enumerate(result.get("items", [])):
			frappe.logger().debug(
				f"[scan_invoice] item {idx}: name={item.get('item_name')} "
				f"qty={item.get('qty')} rate={item.get('rate')} uncertain={item.get('uncertain')}"
			)
	except json.JSONDecodeError as e:
		frappe.logger().debug(f"[scan_invoice] JSON parse FAILED: {e}")
		frappe.logger().debug(
			f"[scan_invoice] failed response text (first 2000 chars):\n{response_text[:2000]}"
		)
		frappe.log_error(f"Gemini raw response:\n{response_text}", "Gemini JSON parse error")
		frappe.throw(f"Gemini returned an unexpected response. Raw output:\n\n{response_text}")

	raw_items_json = json.dumps(result.get("items", []))

	match_failed = False
	if result.get("items"):
		frappe.logger().debug("[scan_invoice] starting catalogue match pass")
		result["items"], match_failed = _match_items_to_catalogue(result["items"], api_key, model)
		frappe.logger().debug(f"[scan_invoice] catalogue match done, match_failed={match_failed}")
	else:
		frappe.logger().debug("[scan_invoice] no items extracted, skipping catalogue match")

	duration_ms = int((time.time() - t_start) * 1000)
	items = result.get("items", [])

	frappe.logger().debug(f"[scan_invoice] total duration={duration_ms}ms")

	log = frappe.get_doc(
		{
			"doctype": "Invoice Scan Log",
			"gemini_model": model,
			"extracted_supplier": result.get("supplier"),
			"extracted_date": result.get("date"),
			"raw_extraction": raw_items_json,
			"matched_items": json.dumps(items),
			"uncertain_count": sum(1 for i in items if i.get("uncertain")),
			"unmatched_count": sum(1 for i in items if not i.get("item_code")),
			"match_pass_failed": 1 if match_failed else 0,
			"duration_ms": duration_ms,
		}
	)
	log.insert(ignore_permissions=True)
	frappe.logger().debug(f"[scan_invoice] scan log created: {log.name}")

	result["scan_log"] = log.name
	return result


@frappe.whitelist()
def create_sales_invoice(items, customer=None, posting_date=None, scan_log=None):
	"""
	Create a draft Sales Invoice from the extracted line items.

	Args:
	    items (str | list): JSON string or list of dicts with keys:
	                         item_name, item_code (optional), qty, rate.
	    customer (str | None): Customer name. If omitted, a placeholder is used.
	    posting_date (str | None): Date in YYYY-MM-DD format.
	    scan_log (str | None): Invoice Scan Log name to link back to.

	Returns:
	    dict: {"name": "<SI name>"}
	"""
	frappe.logger().debug(
		f"[create_sales_invoice] customer={customer} posting_date={posting_date} scan_log={scan_log}"
	)

	if isinstance(items, str):
		items = frappe.parse_json(items)

	frappe.logger().debug(f"[create_sales_invoice] items count={len(items)}")

	invoice_items = []
	for idx, item in enumerate(items):
		row = {
			"item_name": item.get("item_name", "Unknown Item"),
			"description": item.get("item_name", "Unknown Item"),
			"qty": float(item.get("qty") or 1),
			"rate": float(item.get("rate") or 0),
			"uom": "Nos",
		}
		if item.get("item_code"):
			row["item_code"] = item["item_code"]
		frappe.logger().debug(
			f"[create_sales_invoice] row {idx}: item_code={row.get('item_code')} "
			f"item_name={row['item_name']} qty={row['qty']} rate={row['rate']}"
		)
		invoice_items.append(row)

	doc_fields = {
		"doctype": "Sales Invoice",
		"items": invoice_items,
	}

	if customer:
		doc_fields["customer"] = customer
	if posting_date:
		doc_fields["posting_date"] = posting_date
	doc_fields["due_date"] = posting_date or frappe.utils.today()

	frappe.logger().debug(
		f"[create_sales_invoice] inserting Sales Invoice with fields: {list(doc_fields.keys())}"
	)

	doc = frappe.get_doc(doc_fields)
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)

	frappe.logger().debug(f"[create_sales_invoice] Sales Invoice created: {doc.name}")

	if scan_log:
		frappe.db.set_value("Invoice Scan Log", scan_log, "sales_invoice", doc.name)
		frappe.logger().debug(f"[create_sales_invoice] linked scan log {scan_log} -> {doc.name}")

	frappe.db.commit()

	return {"name": doc.name}

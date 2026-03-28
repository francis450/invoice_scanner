import re
import json
import time
import requests
import frappe


def _gemini_text(prompt, api_key, model):
	"""Single text-only Gemini call. Returns the raw response string."""
	url = (
		"https://generativelanguage.googleapis.com/v1beta/models/"
		f"{model}:generateContent?key={api_key}"
	)
	payload = {
		"contents": [{"parts": [{"text": prompt}]}],
		"generationConfig": {"maxOutputTokens": 2000, "temperature": 0},
	}
	resp = requests.post(url, json=payload, timeout=30)
	resp.raise_for_status()
	return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def _match_items_to_catalogue(extracted_items, api_key, model):
	"""
	Second-pass: ask Gemini to map each extracted item name to the closest
	real ERPNext item. Returns (items, match_failed).
	"""
	catalogue = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["item_code", "item_name"],
		limit=500,
		order_by="item_name asc",
	)
	if not catalogue:
		return extracted_items, False

	catalogue_lines = "\n".join(
		f"{i.item_code} | {i.item_name}" for i in catalogue
	)
	extracted_names = "\n".join(
		f"{idx + 1}. {item.get('item_name', '')}"
		for idx, item in enumerate(extracted_items)
	)

	prompt = (
		"Match each extracted invoice item to the closest item in the catalogue below.\n\n"
		"Extracted items:\n" + extracted_names + "\n\n"
		"Catalogue (item_code | item_name):\n" + catalogue_lines + "\n\n"
		"Rules:\n"
		"- Return ONLY a JSON array, one entry per extracted item, in the same order.\n"
		"- Each entry: {\"item_code\": \"<code>\", \"item_name\": \"<catalogue name>\"}\n"
		"- If no catalogue item is a reasonable match, use {\"item_code\": null, \"item_name\": \"<original extracted name>\"}\n"
		"- Prefer exact or near-exact matches. Do not guess unrelated items."
	)

	try:
		response_text = _gemini_text(prompt, api_key, model)
		fence = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", response_text)
		if fence:
			response_text = fence.group(1)
		matches = json.loads(response_text)
	except Exception:
		return extracted_items, True

	for idx, item in enumerate(extracted_items):
		if idx < len(matches) and matches[idx].get("item_code"):
			item["item_code"] = matches[idx]["item_code"]
			item["item_name"] = matches[idx].get("item_name", item["item_name"])

	return extracted_items, False


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
	api_key = frappe.conf.get("gemini_api_key")
	if not api_key:
		frappe.throw(
			"Gemini API key not configured. "
			"Add 'gemini_api_key' to your site_config.json."
		)

	prompt = (
		"Extract all line items from this handwritten sales invoice. "
		"Return ONLY a JSON object — no markdown, no extra text — with this exact structure:\n"
		'{"supplier": "<name or null>", "date": "<YYYY-MM-DD or null>", '
		'"items": [{"item_name": "<name>", "qty": <number>, "rate": <number>, "uncertain": <true|false>}]}\n'
		"If a quantity or rate is illegible, use your best guess and set uncertain: true for that item. "
		"All monetary values should be numbers without currency symbols."
	)

	model = frappe.conf.get("gemini_model", "gemini-2.5-flash")
	url = (
		"https://generativelanguage.googleapis.com/v1beta/models/"
		f"{model}:generateContent?key={api_key}"
	)
	payload = {
		"contents": [
			{
				"parts": [
					{"inline_data": {"mime_type": media_type, "data": image_b64}},
					{"text": prompt},
				]
			}
		],
		"generationConfig": {"maxOutputTokens": 1000, "temperature": 0.1},
	}

	t_start = time.time()

	resp = None
	for attempt in range(3):
		try:
			resp = requests.post(url, json=payload, timeout=60)
			if resp.status_code == 429:
				try:
					retry_after = float(resp.headers.get("Retry-After", 30 * (attempt + 1)))
				except (ValueError, TypeError):
					retry_after = 30 * (attempt + 1)
				time.sleep(retry_after)
				continue
			resp.raise_for_status()
			break
		except requests.exceptions.RequestException as e:
			if attempt == 2:
				frappe.log_error(frappe.get_traceback(), "Gemini API request failed")
				frappe.throw(f"Gemini API request failed: {e}")
			time.sleep(10 * (attempt + 1))
	else:
		frappe.throw("Gemini API is rate-limiting this key. Please wait a moment and try again.")

	response_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

	# Strip markdown code fences if Gemini wraps the JSON
	match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", response_text)
	if match:
		response_text = match.group(1)

	try:
		result = json.loads(response_text)
	except json.JSONDecodeError:
		frappe.log_error(f"Gemini raw response:\n{response_text}", "Gemini JSON parse error")
		frappe.throw(
			f"Gemini returned an unexpected response. Raw output:\n\n{response_text}"
		)

	raw_items_json = json.dumps(result.get("items", []))

	match_failed = False
	if result.get("items"):
		result["items"], match_failed = _match_items_to_catalogue(result["items"], api_key, model)

	duration_ms = int((time.time() - t_start) * 1000)
	items = result.get("items", [])

	log = frappe.get_doc({
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
	})
	log.insert(ignore_permissions=True)

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
	if isinstance(items, str):
		items = frappe.parse_json(items)

	invoice_items = []
	for item in items:
		row = {
			"item_name": item.get("item_name", "Unknown Item"),
			"description": item.get("item_name", "Unknown Item"),
			"qty": float(item.get("qty") or 1),
			"rate": float(item.get("rate") or 0),
			"uom": "Nos",
		}
		if item.get("item_code"):
			row["item_code"] = item["item_code"]
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

	doc = frappe.get_doc(doc_fields)
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True)

	if scan_log:
		frappe.db.set_value("Invoice Scan Log", scan_log, "sales_invoice", doc.name)

	frappe.db.commit()

	return {"name": doc.name}

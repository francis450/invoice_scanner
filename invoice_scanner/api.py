import re
import json
import time
import requests
import frappe


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
	        "items": [
	            {"item_name": str, "qty": float, "rate": float, "uncertain": bool}
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

	url = (
		"https://generativelanguage.googleapis.com/v1beta/models/"
		f"gemini-2.0-flash:generateContent?key={api_key}"
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

	resp = None
	for attempt in range(3):
		try:
			resp = requests.post(url, json=payload, timeout=60)
			if resp.status_code == 429:
				retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
				time.sleep(retry_after)
				continue
			resp.raise_for_status()
			break
		except requests.exceptions.RequestException as e:
			if attempt == 2:
				frappe.log_error(frappe.get_traceback(), "Gemini API request failed")
				frappe.throw(f"Gemini API request failed: {e}")
			time.sleep(2 ** (attempt + 1))
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

	return result


@frappe.whitelist()
def create_sales_invoice(items, customer=None, posting_date=None):
	"""
	Create a draft Sales Invoice from the extracted line items.

	Args:
	    items (str | list): JSON string or list of dicts with keys:
	                         item_name, qty, rate.
	    customer (str | None): Customer name. If omitted, a placeholder is used.
	    posting_date (str | None): Date in YYYY-MM-DD format.

	Returns:
	    dict: {"name": "<SI name>"}
	"""
	if isinstance(items, str):
		items = frappe.parse_json(items)

	invoice_items = []
	for item in items:
		invoice_items.append(
			{
				"item_name": item.get("item_name", "Unknown Item"),
				"description": item.get("item_name", "Unknown Item"),
				"qty": float(item.get("qty") or 1),
				"rate": float(item.get("rate") or 0),
				"uom": "Nos",
			}
		)

	doc_fields = {
		"doctype": "Sales Invoice",
		"items": invoice_items,
	}

	if customer:
		doc_fields["customer"] = customer
	if posting_date:
		doc_fields["posting_date"] = posting_date

	doc = frappe.get_doc(doc_fields)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"name": doc.name}

frappe.pages["scan-invoice"].on_page_load = function (wrapper) {
	frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Scan Invoice"),
		single_column: true,
	});

	// Build HTML inline — avoids Frappe's scrub_html_template converting single-quotes
	// to typographic quotes, which breaks generated JS inside {{ }} template expressions.
	wrapper.querySelector(".page-content").innerHTML = `
		<div class="scan-invoice-page">

			<div id="camera-section">
				<div class="section-header">
					<h4>${__("Scan Invoice")}</h4>
					<p class="text-muted">${__("Take a photo or upload an image of a handwritten invoice")}</p>
				</div>
				<div class="camera-wrapper">
					<video id="camera-feed" autoplay playsinline></video>
					<canvas id="capture-canvas" style="display:none;"></canvas>
				</div>
				<div class="camera-controls">
					<button id="btn-start-camera" class="btn btn-default btn-sm">${__("Start Camera")}</button>
					<button id="btn-capture" class="btn btn-primary btn-sm" style="display:none;">${__("Capture")}</button>
					<span class="text-muted" style="margin:0 8px;">${__("or")}</span>
					<label class="btn btn-default btn-sm" style="cursor:pointer;margin:0;">
						${__("Upload Image")}
						<input type="file" id="file-input" accept="image/*" style="display:none;">
					</label>
				</div>
			</div>

			<div id="loading-section" style="display:none;text-align:center;padding:40px 0;">
				<div class="spinner-border text-primary" role="status" style="width:2rem;height:2rem;"></div>
				<p class="text-muted" style="margin-top:12px;">${__("Analysing invoice with Gemini AI\u2026")}</p>
			</div>

			<div id="preview-section" style="display:none;">
				<div class="preview-layout">
					<div class="preview-image-col">
						<img id="preview-img" src="" alt="Invoice preview"
							style="max-width:100%;border-radius:6px;border:1px solid var(--border-color);">
					</div>
					<div class="preview-items-col">
						<div class="section-header" style="margin-bottom:12px;">
							<h5>${__("Extracted Items")}</h5>
							<p class="text-muted small">${__("Review and correct before creating the invoice. Highlighted rows were flagged as uncertain.")}</p>
						</div>
						<div class="invoice-meta" style="margin-bottom:12px;display:flex;gap:12px;flex-wrap:wrap;">
							<div>
								<label class="control-label small">${__("Customer")}</label>
								<input type="text" id="field-customer" class="form-control form-control-sm"
									placeholder="${__("Customer name")}">
							</div>
							<div>
								<label class="control-label small">${__("Date")}</label>
								<input type="date" id="field-date" class="form-control form-control-sm">
							</div>
						</div>
						<table class="table table-sm items-table" id="items-table">
							<thead>
								<tr>
									<th>${__("Item Name")}</th>
									<th style="width:80px;">${__("Qty")}</th>
									<th style="width:100px;">${__("Rate")}</th>
									<th style="width:80px;">${__("Amount")}</th>
									<th style="width:40px;"></th>
								</tr>
							</thead>
							<tbody id="items-tbody"></tbody>
						</table>
						<button id="btn-add-row" class="btn btn-xs btn-default" style="margin-bottom:16px;">
							+ ${__("Add Row")}
						</button>
						<div style="display:flex;gap:8px;">
							<button id="btn-create-invoice" class="btn btn-primary">${__("Create Draft Invoice")}</button>
							<button id="btn-rescan" class="btn btn-default">${__("Scan Another")}</button>
						</div>
					</div>
				</div>
			</div>

		</div>
	`;

	// Add page-level CSS
	frappe.dom.set_style(`
		.scan-invoice-page { max-width: 960px; margin: 0 auto; padding: 24px 16px; }
		.section-header { margin-bottom: 16px; }
		.camera-wrapper { background: #000; border-radius: 8px; overflow: hidden;
		                  max-width: 480px; margin-bottom: 12px; min-height: 200px;
		                  display: flex; align-items: center; justify-content: center; }
		#camera-feed { width: 100%; display: block; }
		.camera-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }
		.preview-layout { display: flex; gap: 24px; flex-wrap: wrap; }
		.preview-image-col { flex: 0 0 300px; }
		.preview-items-col { flex: 1 1 400px; }
		.items-table td { vertical-align: middle; }
		.items-table input { border: none; background: transparent; width: 100%; padding: 2px 4px; }
		.items-table input:focus { background: var(--input-bg); border: 1px solid var(--border-color); border-radius: 4px; outline: none; }
		.row-uncertain { background-color: #fffbeb !important; }
		[data-theme="dark"] .row-uncertain { background-color: #3d3000 !important; }
	`);

	var state = {
		stream: null,
		imageB64: null,
		mediaType: "image/jpeg",
		scanLog: null,
	};

	// ─── DOM references ────────────────────────────────────────────────────────
	var $cameraSection  = wrapper.querySelector("#camera-section");
	var $loadingSection = wrapper.querySelector("#loading-section");
	var $previewSection = wrapper.querySelector("#preview-section");
	var $video          = wrapper.querySelector("#camera-feed");
	var $canvas         = wrapper.querySelector("#capture-canvas");
	var $fileInput      = wrapper.querySelector("#file-input");
	var $previewImg     = wrapper.querySelector("#preview-img");
	var $tbody          = wrapper.querySelector("#items-tbody");
	var $customerField  = wrapper.querySelector("#field-customer");
	var $dateField      = wrapper.querySelector("#field-date");

	// ─── Camera ────────────────────────────────────────────────────────────────
	wrapper.querySelector("#btn-start-camera").addEventListener("click", function () {
		if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
			frappe.msgprint(__("Camera access is not supported in this browser."));
			return;
		}
		navigator.mediaDevices
			.getUserMedia({ video: { facingMode: "environment" } })
			.then(function (stream) {
				state.stream = stream;
				$video.srcObject = stream;
				$video.style.display = "block";
				wrapper.querySelector("#btn-start-camera").style.display = "none";
				wrapper.querySelector("#btn-capture").style.display = "inline-block";
			})
			.catch(function (err) {
				frappe.msgprint(__("Could not access camera: {0}", [err.message]));
			});
	});

	wrapper.querySelector("#btn-capture").addEventListener("click", function () {
		$canvas.width  = $video.videoWidth;
		$canvas.height = $video.videoHeight;
		$canvas.getContext("2d").drawImage($video, 0, 0);
		var dataUrl = $canvas.toDataURL("image/jpeg", 0.85);
		handleImageDataUrl(dataUrl, "image/jpeg");
		stopCamera();
	});

	// ─── File upload ───────────────────────────────────────────────────────────
	$fileInput.addEventListener("change", function () {
		var file = $fileInput.files[0];
		if (!file) return;
		var mediaType = file.type || "image/jpeg";
		var reader = new FileReader();
		reader.onload = function (e) {
			handleImageDataUrl(e.target.result, mediaType);
		};
		reader.readAsDataURL(file);
	});

	// ─── Core: send to Gemini ──────────────────────────────────────────────────
	function handleImageDataUrl(dataUrl, mediaType) {
		// Strip the data URI prefix: "data:image/jpeg;base64,"
		var b64 = dataUrl.split(",")[1];
		state.imageB64  = b64;
		state.mediaType = mediaType;
		$previewImg.src = dataUrl;

		show($loadingSection);
		hide($cameraSection);
		hide($previewSection);

		frappe.call({
			method: "invoice_scanner.api.scan_invoice",
			args: { image_b64: b64, media_type: mediaType },
			callback: function (r) {
				hide($loadingSection);
				if (r.exc) {
					show($cameraSection);
					return;
				}
				var data = r.message;
				state.scanLog = data.scan_log || null;
				if (data.supplier) $customerField.value = data.supplier;
				if (data.date)     $dateField.value     = data.date;
				renderItemsTable(data.items || []);
				show($previewSection);
			},
			error: function () {
				hide($loadingSection);
				show($cameraSection);
			},
		});
	}

	// ─── Items table ───────────────────────────────────────────────────────────
	function renderItemsTable(items) {
		$tbody.innerHTML = "";
		if (!items.length) {
			addRow("", 1, 0, false, null);
		} else {
			items.forEach(function (item) {
				addRow(item.item_name, item.qty, item.rate, item.uncertain, item.item_code || null);
			});
		}
		updateAmounts();
	}

	function addRow(itemName, qty, rate, uncertain, itemCode) {
		var tr = document.createElement("tr");
		if (uncertain) tr.classList.add("row-uncertain");
		tr.dataset.itemCode = itemCode || "";
		tr.innerHTML = `
			<td><input type="text"   class="col-name" value="${escHtml(itemName || "")}" placeholder="${__("Item name")}"></td>
			<td><input type="number" class="col-qty"  value="${qty  || 1}"  min="0" step="any"></td>
			<td><input type="number" class="col-rate" value="${rate || 0}"  min="0" step="any"></td>
			<td class="col-amount">0.00</td>
			<td><button class="btn btn-xs btn-danger btn-remove-row">\u2715</button></td>
		`;

		// Recalculate amount on qty/rate change; clear uncertain styling on edit
		tr.querySelectorAll("input").forEach(function (input) {
			input.addEventListener("input", function () {
				tr.classList.remove("row-uncertain");
				updateAmounts();
			});
		});

		tr.querySelector(".btn-remove-row").addEventListener("click", function () {
			tr.remove();
			updateAmounts();
		});

		$tbody.appendChild(tr);
	}

	wrapper.querySelector("#btn-add-row").addEventListener("click", function () {
		addRow("", 1, 0, false);
	});

	function updateAmounts() {
		$tbody.querySelectorAll("tr").forEach(function (tr) {
			var qty  = parseFloat(tr.querySelector(".col-qty").value)  || 0;
			var rate = parseFloat(tr.querySelector(".col-rate").value) || 0;
			tr.querySelector(".col-amount").textContent = (qty * rate).toFixed(2);
		});
	}

	// ─── Create invoice ────────────────────────────────────────────────────────
	wrapper.querySelector("#btn-create-invoice").addEventListener("click", function () {
		var rows = [];
		var valid = true;

		$tbody.querySelectorAll("tr").forEach(function (tr) {
			var name = tr.querySelector(".col-name").value.trim();
			var qty  = parseFloat(tr.querySelector(".col-qty").value);
			var rate = parseFloat(tr.querySelector(".col-rate").value);
			if (!name) { valid = false; return; }
			var row = { item_name: name, qty: qty || 1, rate: rate || 0 };
			if (tr.dataset.itemCode) row.item_code = tr.dataset.itemCode;
			rows.push(row);
		});

		if (!valid || !rows.length) {
			frappe.msgprint(__("Please fill in all item names before creating the invoice."));
			return;
		}

		var btn = wrapper.querySelector("#btn-create-invoice");
		btn.disabled = true;
		btn.textContent = __("Creating\u2026");

		frappe.call({
			method: "invoice_scanner.api.create_sales_invoice",
			args: {
				items: JSON.stringify(rows),
				customer: $customerField.value.trim() || null,
				posting_date: $dateField.value || null,
				scan_log: state.scanLog || null,
			},
			callback: function (r) {
				btn.disabled = false;
				btn.textContent = __("Create Draft Invoice");
				if (r.exc) return;
				frappe.set_route("Form", "Sales Invoice", r.message.name);
			},
			error: function () {
				btn.disabled = false;
				btn.textContent = __("Create Draft Invoice");
			},
		});
	});

	// ─── Rescan ────────────────────────────────────────────────────────────────
	wrapper.querySelector("#btn-rescan").addEventListener("click", function () {
		$fileInput.value = "";
		$customerField.value = "";
		$dateField.value = "";
		$tbody.innerHTML = "";
		$previewImg.src = "";
		state.imageB64 = null;
		state.scanLog = null;
		$video.style.display = "none";
		wrapper.querySelector("#btn-capture").style.display = "none";
		wrapper.querySelector("#btn-start-camera").style.display = "inline-block";
		hide($previewSection);
		hide($loadingSection);
		show($cameraSection);
	});

	// ─── Helpers ───────────────────────────────────────────────────────────────
	function show(el) { el.style.display = ""; }
	function hide(el) { el.style.display = "none"; }
	function stopCamera() {
		if (state.stream) {
			state.stream.getTracks().forEach(function (t) { t.stop(); });
			state.stream = null;
		}
	}
	function escHtml(str) {
		return String(str)
			.replace(/&/g, "&amp;")
			.replace(/"/g, "&quot;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;");
	}

	// Stop camera if the user navigates away
	$(wrapper).on("hide", stopCamera);
};

const fs = require('fs');
let html = fs.readFileSync('templates/em_admin.html', 'utf8');

// 1. Add Edit Event Modal after Create Event Modal
const createModalEnd = html.indexOf('<!-- Create Admin Modal -->');
const editModalHTML = `
<!-- Edit Event Modal -->
<div id="editEventModal" class="em-modal">
  <div class="em-modal-box">
    <h3>Edit Event</h3>
    <form id="editEventForm" enctype="multipart/form-data" class="form-em">
      <input type="hidden" name="event_id" id="editEventId">
      <div class="form-group-em"><label>Event Title *</label><input type="text" name="title" id="editTitle" required></div>
      <div class="form-grid-2">
        <div class="form-group-em"><label>Date *</label><input type="date" name="date" id="editDate" required></div>
        <div class="form-group-em"><label>Time</label><input type="time" name="time" id="editTime"></div>
      </div>
      <div class="form-group-em"><label>Venue</label><input type="text" name="venue" id="editVenue"></div>
      
      <div class="form-grid-2">
        <div class="form-group-em">
          <label>Event Type</label>
          <select name="event_type" id="editEventType" onchange="toggleEditPriceField()">
            <option value="free">Free</option>
            <option value="paid">Paid</option>
          </select>
        </div>
        <div class="form-group-em" id="editPriceField" style="display:none;">
          <label>Ticket Price (₹)</label>
          <input type="number" name="ticket_price" id="editTicketPrice" min="1">
        </div>
      </div>
      
      <div id="editGatewayFields" class="form-grid-2" style="display:none; margin-top:0.5rem; background:rgba(99,102,241,0.05); padding:1rem; border-radius:10px;">
        <div class="form-group-em"><label>Razorpay Key ID (Optional for specific gateway)</label><input type="text" name="razorpay_key_id" id="editGatewayKey"></div>
        <div class="form-group-em"><label>Razorpay Key Secret (Optional)</label><input type="password" name="razorpay_key_secret" id="editGatewaySecret"></div>
      </div>

      <div class="form-group-em"><label>Description</label><textarea name="description" id="editDescription" rows="3"></textarea></div>
      <div class="form-group-em"><label>Max Capacity</label><input type="number" name="max_capacity" id="editMaxCapacity"></div>
      <div class="form-group-em"><label>Event Banner (Optional)</label><input type="file" name="banner" accept="image/*"></div>
      
      <div style="display:flex;gap:.75rem;justify-content:flex-end;margin-top:.5rem;">
        <button type="button" class="btn-sm" onclick="closeModal('editEventModal')" style="border:1px solid rgba(255,255,255,.15);color:rgba(255,255,255,.6);background:none;padding:.65rem 1.25rem;border-radius:10px;cursor:pointer;">Cancel</button>
        <button type="submit" style="background:linear-gradient(135deg,var(--em-indigo),var(--em-purple));border:none;border-radius:10px;padding:.65rem 1.5rem;color:#fff;font-weight:700;cursor:pointer;">Update Event</button>
      </div>
    </form>
  </div>
</div>
`;

html = html.slice(0, createModalEnd) + editModalHTML + html.slice(createModalEnd);

// 2. Add Razorpay fields to Create Event Modal
const createEventPriceField = `<div class="form-group-em" id="priceField" style="display:none;">
          <label>Ticket Price (₹)</label>
          <input type="number" name="ticket_price" placeholder="150" min="1">
        </div>
      </div>`;
html = html.replace(createEventPriceField, createEventPriceField + `
      <div id="gatewayFields" class="form-grid-2" style="display:none; margin-top:0.5rem; background:rgba(99,102,241,0.05); padding:1rem; border-radius:10px;">
        <div class="form-group-em"><label>Razorpay Key ID (Optional for specific gateway)</label><input type="text" name="razorpay_key_id" placeholder="Your key_id"></div>
        <div class="form-group-em"><label>Razorpay Key Secret (Optional)</label><input type="password" name="razorpay_key_secret" placeholder="Your key_secret"></div>
      </div>`);

// Modify togglePriceField to also show gateway fields, and add toggleEditPriceField
const jsFunctions = `function togglePriceField() {
  const t = document.getElementById('eventTypeSelect').value;
  document.getElementById('priceField').style.display = t === 'paid' ? 'block' : 'none';
  document.getElementById('gatewayFields').style.display = t === 'paid' ? 'flex' : 'none';
}
function toggleEditPriceField() {
  const t = document.getElementById('editEventType').value;
  document.getElementById('editPriceField').style.display = t === 'paid' ? 'block' : 'none';
  document.getElementById('editGatewayFields').style.display = t === 'paid' ? 'flex' : 'none';
}
function openEditEventModal(e) {
  document.getElementById('editEventId').value = e.id;
  document.getElementById('editTitle').value = e.title || '';
  document.getElementById('editDate').value = e.date || '';
  document.getElementById('editTime').value = e.time || '';
  document.getElementById('editVenue').value = e.venue || '';
  document.getElementById('editEventType').value = e.event_type || 'free';
  document.getElementById('editTicketPrice').value = e.ticket_price || '';
  document.getElementById('editDescription').value = e.description || '';
  document.getElementById('editMaxCapacity').value = e.max_capacity || '';
  document.getElementById('editGatewayKey').value = e.razorpay_key_id || '';
  document.getElementById('editGatewaySecret').value = e.razorpay_key_secret || '';
  toggleEditPriceField();
  openModal('editEventModal');
}
`;
html = html.replace("function togglePriceField() {", jsFunctions + "/*");
html = html.replace("document.getElementById('priceField').style.display = t === 'paid' ? 'block' : 'none';\n}", "*/");

const submitEditJS = `
document.getElementById('editEventForm').onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const id = document.getElementById('editEventId').value;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const r = await fetch('/em/api/events/update/' + id, { method: 'POST', body: fd, headers: {'X-CSRFToken': csrf} });
  const d = await r.json();
  if (d.success) { showToast('✅ Event updated!'); closeModal('editEventModal'); setTimeout(() => location.reload(), 1000); }
  else showToast('❌ Failed', false);
};
`;
const createSubmitIndex = html.indexOf('// Cancel Event');
html = html.slice(0, createSubmitIndex) + submitEditJS + html.slice(createSubmitIndex);

fs.writeFileSync('templates/em_admin.html', html);

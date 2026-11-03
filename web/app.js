const state = {
  schedules: [],
  selectedId: null,
  seats: [],
  selectedSeats: new Set(),
  stream: null,
  bookings: loadBookings(),
  toastTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const scheduleGrid = $("#schedule-grid");
const seatGridWrap = $("#seat-grid-wrap");
const bookingButton = $("#book-button");
const moviePosters = {
  "jadwal-1": {
    src: "/assets/poster-langit-senja.svg",
    alt: "Poster ilustrasi film Langit Senja",
  },
  "jadwal-2": {
    src: "/assets/poster-petualangan-rimba.svg",
    alt: "Poster ilustrasi film Petualangan Rimba",
  },
};

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function loadBookings() {
  try {
    const stored = JSON.parse(localStorage.getItem("layar-bookings") || "[]");
    return Array.isArray(stored) ? stored : [];
  } catch (error) {
    console.warn("Riwayat pesanan lokal tidak dapat dibaca.", error);
    return [];
  }
}

function saveBookings() {
  try {
    localStorage.setItem("layar-bookings", JSON.stringify(state.bookings));
  } catch (error) {
    console.warn("Riwayat pesanan tidak dapat disimpan di browser ini.", error);
    showToast("Pesanan berhasil, tetapi riwayat lokal tidak dapat disimpan.", "error");
  }
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
  } catch (error) {
    throw new Error("Tidak dapat terhubung ke server. Pastikan server aplikasi sedang berjalan.");
  }
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : {};
  if (!response.ok) {
    throw new Error(payload.pesan || "Permintaan tidak dapat diproses.");
  }
  return payload;
}

function setConnection(connected, message) {
  const label = $("#connection-label");
  label.textContent = message || (connected ? "Terhubung" : "Tidak terhubung");
  $(".live-status .pulse-dot").classList.toggle("offline", !connected);
}

function showToast(message, kind = "") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show ${kind}`;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    toast.className = "toast";
  }, 3600);
}

function currentSchedule() {
  return state.schedules.find((schedule) => schedule.jadwal_id === state.selectedId);
}

function escapeQuery(value) {
  return encodeURIComponent(value);
}

function renderSchedules() {
  if (!state.schedules.length) {
    scheduleGrid.innerHTML = '<div class="error-card">Belum ada jadwal yang tersedia.</div>';
    return;
  }
  scheduleGrid.innerHTML = state.schedules.map((schedule, index) => {
    const total = schedule.baris * schedule.kolom;
    const occupied = schedule.jadwal_id === state.selectedId
      ? state.seats.filter((seat) => seat.status === "dipesan").length
      : null;
    const availableText = occupied === null
      ? `${total} kursi`
      : `<strong>${total - occupied}</strong> kursi`;
    const poster = moviePosters[schedule.jadwal_id];
    return `
      <article class="schedule-card ${schedule.jadwal_id === state.selectedId ? "selected" : ""}"
        data-schedule-id="${escapeHtml(schedule.jadwal_id)}" tabindex="0" role="button"
        aria-pressed="${schedule.jadwal_id === state.selectedId}">
        <div class="movie-art ${poster ? "" : `poster-${(index % 4) + 1}`}">${poster
          ? `<img class="movie-poster" src="${poster.src}" alt="${escapeHtml(poster.alt)}">`
          : '<span class="poster-silhouette" aria-hidden="true"></span>'}</div>
        <div class="movie-info">
          <span class="movie-tag">${index % 2 === 0 ? "Pilihan Layar" : "Petualangan"}</span>
          <h3>${escapeHtml(schedule.film)}</h3>
          <div class="movie-meta"><svg><use href="#i-film"></use></svg>${schedule.baris} baris · ${schedule.kolom} kursi</div>
          <div class="movie-time"><span class="time-chip"><svg><use href="#i-clock"></use></svg>${escapeHtml(schedule.jam)}</span><span class="availability">${availableText}</span></div>
        </div>
        <span class="card-arrow"><svg><use href="#i-arrow"></use></svg></span>
      </article>`;
  }).join("");

  scheduleGrid.querySelectorAll(".schedule-card").forEach((card) => {
    const select = () => selectSchedule(card.dataset.scheduleId);
    card.addEventListener("click", select);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });
  });
}

async function selectSchedule(scheduleId) {
  if (!state.schedules.some((schedule) => schedule.jadwal_id === scheduleId)) return;
  state.selectedId = scheduleId;
  state.selectedSeats.clear();
  renderSchedules();
  renderBookingSummary();
  loadSeats();
  connectToSeatEvents();
}

async function loadSchedules() {
  scheduleGrid.innerHTML = '<div class="loading-card"><span class="loader"></span> Memuat jadwal film...</div>';
  try {
    const result = await api("/api/jadwal");
    state.schedules = result.jadwal;
    $("#schedule-count").textContent = state.schedules.length;
    $("#stat-films").textContent = new Set(state.schedules.map((item) => item.film)).size;
    $("#stat-shows").textContent = state.schedules.length;
    renderSchedules();
    renderBookings();
    if (state.schedules.length) {
      const activeId = state.schedules.some((item) => item.jadwal_id === state.selectedId)
        ? state.selectedId
        : state.schedules[0].jadwal_id;
      await selectSchedule(activeId);
    } else {
      state.selectedId = null;
      state.seats = [];
      renderBookingSummary();
    }
    setConnection(true, "Terhubung");
  } catch (error) {
    scheduleGrid.innerHTML = `<div class="error-card"><span>${escapeHtml(error.message)}</span><button class="retry-inline" id="retry-load">Coba lagi</button></div>`;
    $("#retry-load").addEventListener("click", loadSchedules);
    setConnection(false, "Server tidak terhubung");
    $("#stat-films").textContent = "—";
    $("#stat-shows").textContent = "—";
    $("#stat-seats").textContent = "—";
  }
}

async function loadSeats() {
  if (!state.selectedId) return;
  try {
    const result = await api(`/api/denah?jadwal_id=${escapeQuery(state.selectedId)}`);
    if (result.jadwal_id !== state.selectedId) return;
    state.seats = result.kursi;
    state.selectedSeats = new Set([...state.selectedSeats].filter((code) =>
      state.seats.some((seat) => seat.kode === code && seat.status === "tersedia")));
    renderSeats();
    renderSchedules();
    setConnection(true, "Terhubung");
  } catch (error) {
    seatGridWrap.innerHTML = `<div class="seat-empty"><strong>Denah belum tersedia</strong><span>${escapeHtml(error.message)}</span></div>`;
    setConnection(false, "Koneksi terganggu");
  }
}

function connectToSeatEvents() {
  if (state.stream) state.stream.close();
  if (!state.selectedId || !("EventSource" in window)) return;
  const stream = new EventSource(`/api/events?jadwal_id=${escapeQuery(state.selectedId)}`);
  state.stream = stream;
  stream.onopen = () => setConnection(true, "Realtime aktif");
  stream.onmessage = (message) => {
    try {
      const update = JSON.parse(message.data);
      if (update.jadwal_id !== state.selectedId) return;
      const seat = state.seats.find((item) => item.kode === update.kode);
      if (seat) seat.status = update.status;
      if (update.status === "dipesan") state.selectedSeats.delete(update.kode);
      renderSeats();
      renderSchedules();
    } catch (error) {
      console.warn("Pembaruan status kursi tidak dapat dibaca.", error);
    }
  };
  stream.addEventListener("error", (event) => {
    if (event.data) {
      try {
        const detail = JSON.parse(event.data);
        showToast(detail.pesan || "Sambungan status kursi terputus.", "error");
      } catch (error) {
        console.warn("Pesan error streaming tidak valid.", error);
      }
    }
  });
  stream.onerror = () => {
    setConnection(false, "Menghubungkan ulang...");
  };
}

function renderSeats() {
  if (!state.selectedId || !state.seats.length) {
    seatGridWrap.innerHTML = '<div class="seat-empty"><div class="empty-seat-icon"><svg><use href="#i-ticket"></use></svg></div><strong>Pilih jadwal film</strong><span>Denah kursi akan tampil di sini.</span></div>';
    $("#stat-seats").textContent = "—";
    $("#stat-seat-caption").textContent = "pilih jadwal film";
    $("#seat-count-label").textContent = "0 kursi dipilih";
    return;
  }
  const rows = new Map();
  state.seats.forEach((seat) => {
    const row = seat.kode.match(/^[A-Z]/)?.[0] || "";
    if (!rows.has(row)) rows.set(row, []);
    rows.get(row).push(seat);
  });
  seatGridWrap.innerHTML = `<div class="seat-grid">${[...rows.entries()].map(([row, seats]) => `
    <div class="seat-row"><span class="row-letter">${escapeHtml(row)}</span>${seats.map((seat) => {
      const selected = state.selectedSeats.has(seat.kode);
      const occupied = seat.status === "dipesan";
      const label = occupied ? `${seat.kode}, sudah dipesan` : `${seat.kode}${selected ? ", dipilih" : ", tersedia"}`;
      return `<button type="button" class="seat-button ${selected ? "selected" : ""}" data-seat="${escapeHtml(seat.kode)}"
        aria-label="${escapeHtml(label)}" aria-pressed="${selected}" ${occupied ? "disabled" : ""}>${escapeHtml(seat.kode)}</button>`;
    }).join("")}</div>`).join("")}</div>`;
  seatGridWrap.querySelectorAll(".seat-button:not(:disabled)").forEach((button) => {
    button.addEventListener("click", () => toggleSeat(button.dataset.seat));
  });
  const available = state.seats.filter((seat) => seat.status === "tersedia").length;
  $("#stat-seats").textContent = available;
  $("#stat-seat-caption").textContent = "untuk jadwal terpilih";
  $("#seat-count-label").textContent = `${state.selectedSeats.size} kursi dipilih`;
  renderBookingSummary();
}

function toggleSeat(code) {
  if (state.selectedSeats.has(code)) state.selectedSeats.delete(code);
  else state.selectedSeats.add(code);
  renderSeats();
}

function renderBookingSummary() {
  const schedule = currentSchedule();
  $("#selected-film-label").textContent = schedule
    ? `${schedule.film} · ${schedule.jam}`
    : "Pilih jadwal terlebih dahulu";
  $("#order-film").textContent = schedule ? schedule.film : "Belum dipilih";
  $("#order-time").textContent = schedule ? schedule.jam : "—";
  $("#order-seats").textContent = state.selectedSeats.size
    ? [...state.selectedSeats].sort().join(", ")
    : "Pilih kursi";
  bookingButton.disabled = !schedule || !state.selectedSeats.size || !$("#customer-name").value.trim();
}

function renderBookings() {
  const container = $("#recent-list");
  if (!state.bookings.length) {
    container.innerHTML = '<div class="recent-empty"><span class="recent-ticket-icon"><svg><use href="#i-ticket"></use></svg></span><div><strong>Belum ada pesanan</strong><p>Pesananmu akan muncul di sini setelah berhasil dibuat.</p></div><a href="#jadwal">Cari film <svg><use href="#i-arrow"></use></svg></a></div>';
    return;
  }
  container.innerHTML = state.bookings.map((booking) => `
    <article class="booking-item">
      <span class="recent-ticket-icon"><svg><use href="#i-ticket"></use></svg></span>
      <div class="booking-copy"><strong>${escapeHtml(booking.film)}</strong>
        <span>${escapeHtml(booking.jam)} · Kursi ${escapeHtml(booking.kursi.join(", "))} · ${escapeHtml(booking.pemesan)}</span>
        <code class="booking-id">${escapeHtml(booking.pesanan_id)}</code>
      </div>
      <button class="cancel-button" type="button" data-booking-id="${escapeHtml(booking.pesanan_id)}">Batalkan</button>
    </article>`).join("");
  container.querySelectorAll(".cancel-button").forEach((button) => {
    button.addEventListener("click", () => cancelBooking(button.dataset.bookingId, button));
  });
}

async function bookSeats() {
  const schedule = currentSchedule();
  const name = $("#customer-name").value.trim();
  if (!schedule || !name || !state.selectedSeats.size) return;
  const selectedAtSubmit = [...state.selectedSeats];
  const requestId = crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  bookingButton.classList.add("is-loading");
  bookingButton.querySelector("span").textContent = "Memproses...";
  bookingButton.disabled = true;
  try {
    const result = await api("/api/pesan", {
      method: "POST",
      body: JSON.stringify({
        jadwal_id: schedule.jadwal_id,
        pemesan: name,
        kode_kursi: selectedAtSubmit,
        request_id: requestId,
      }),
    });
    state.bookings.unshift({
      pesanan_id: result.pesanan_id,
      jadwal_id: schedule.jadwal_id,
      film: schedule.film,
      jam: schedule.jam,
      kursi: selectedAtSubmit,
      pemesan: name,
    });
    state.bookings = state.bookings.slice(0, 10);
    saveBookings();
    state.selectedSeats.clear();
    renderBookings();
    showToast(`Berhasil! Kursi ${selectedAtSubmit.join(", ")} sudah dipesan.`, "success");
    await loadSeats();
    document.querySelector("#pesanan").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showToast(error.message, "error");
    await loadSeats();
  } finally {
    bookingButton.classList.remove("is-loading");
    bookingButton.querySelector("span").textContent = "Pesan kursi";
    renderBookingSummary();
  }
}

async function cancelBooking(bookingId, button) {
  button.disabled = true;
  button.textContent = "Memproses...";
  const requestId = crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
    const result = await api("/api/batal", {
      method: "POST",
      body: JSON.stringify({ pesanan_id: bookingId, request_id: requestId }),
    });
    state.bookings = state.bookings.filter((booking) => booking.pesanan_id !== bookingId);
    saveBookings();
    renderBookings();
    showToast(result.pesan || "Pesanan berhasil dibatalkan.", "success");
    await loadSeats();
  } catch (error) {
    showToast(error.message, "error");
    button.disabled = false;
    button.textContent = "Batalkan";
  }
}

$("#customer-name").addEventListener("input", renderBookingSummary);
bookingButton.addEventListener("click", bookSeats);
$("#refresh-button").addEventListener("click", async () => {
  const button = $("#refresh-button");
  button.classList.add("is-loading");
  await loadSchedules();
  button.classList.remove("is-loading");
});
const wibOptions = { timeZone: "Asia/Jakarta" };
const wibDateFormatter = new Intl.DateTimeFormat("id-ID", {
  ...wibOptions,
  weekday: "long",
  day: "numeric",
  month: "long",
});
const wibTimeFormatter = new Intl.DateTimeFormat("id-ID", {
  ...wibOptions,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
function updateWibClock() {
  const now = new Date();
  $("#today-label").textContent = wibDateFormatter.format(now);
  $("#wib-clock").textContent = `UTC+07:00 (WIB) · ${wibTimeFormatter.format(now)}`;
}
updateWibClock();
setInterval(updateWibClock, 1000);
$("#current-year").textContent = new Date().getFullYear();

document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll(".nav-link").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});

window.addEventListener("beforeunload", () => {
  if (state.stream) state.stream.close();
});

loadSchedules();

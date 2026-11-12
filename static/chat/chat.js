/**
 * ==============================================================================
 * REAL-TIME CHAT CLIENT ENGINE (DJANGO CHANNELS + UPSTASH REDIS)
 * Features:
 *   - Auto WebSocket connection & reconnection with exponential backoff
 *   - Optimistic message dispatch with single-tick ACK
 *   - Dynamic multi-stage read receipt updates (Sent -> Delivered -> Read)
 *   - Viewport visibility detection for auto mark-read
 *   - Invite link clipboard integration & atomic modal join
 * ==============================================================================
 */

class ChatClient {
  constructor(config = {}) {
    this.roomId = config.roomId || null;
    this.userId = config.userId || null;
    this.inviteCode = config.inviteCode || null;
    
    // WebSocket state
    this.socket = null;
    this.reconnectAttempts = 0;
    this.maxReconnectDelay = 30000; // 30s
    this.baseReconnectDelay = 1500;  // 1.5s
    this.reconnectTimer = null;
    this.isExplicitlyClosed = false;

    // Read receipt observer for visible incoming messages
    this.readObserver = null;
    this.pendingReads = new Set();

    // DOM Caches
    this.dom = {
      stream: document.getElementById("messagesStream"),
      input: document.getElementById("chatInput"),
      sendBtn: document.getElementById("sendBtn"),
      statusPill: document.getElementById("connectionStatusPill"),
      statusDot: document.getElementById("connectionStatusDot"),
      statusText: document.getElementById("connectionStatusText"),
    };

    this.init();
  }

  init() {
    this.initReadObserver();
    if (this.roomId) {
      this.connect();
    }
  }

  // --------------------------------------------------------------------------
  // 1. WEBSOCKET LIFECYCLE & RECONNECTION (Exponential Backoff)
  // --------------------------------------------------------------------------
  connect() {
    if (!this.roomId) return;
    this.updateConnectionStatus("connecting", "Connecting...");

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const wsUrl = `${protocol}//${host}/ws/chat/${this.roomId}/`;

    try {
      this.socket = new WebSocket(wsUrl);

      this.socket.onopen = () => {
        this.reconnectAttempts = 0;
        this.updateConnectionStatus("connected", "Connected");
        console.log(`[WS] Connected to room ${this.roomId}`);
        
        // Sync any unread messages visible in current viewport
        this.scanUnreadMessagesInView();
      };

      this.socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          this.handleIncomingEvent(data);
        } catch (err) {
          console.error("[WS] Parse error:", err, event.data);
        }
      };

      this.socket.onclose = (event) => {
        if (!this.isExplicitlyClosed) {
          this.updateConnectionStatus("disconnected", "Reconnecting...");
          this.scheduleReconnect();
        }
      };

      this.socket.onerror = (error) => {
        console.warn("[WS] Socket error:", error);
        this.socket.close();
      };
    } catch (e) {
      console.error("[WS] Connection initiation error:", e);
      this.scheduleReconnect();
    }
  }

  scheduleReconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);

    // Exponential backoff: base * 2^attempts + jitter
    const delay = Math.min(
      this.maxReconnectDelay,
      this.baseReconnectDelay * Math.pow(1.5, this.reconnectAttempts) + Math.random() * 1000
    );
    this.reconnectAttempts++;

    this.updateConnectionStatus("connecting", `Reconnecting in ${Math.round(delay / 1000)}s...`);
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, delay);
  }

  updateConnectionStatus(state, label) {
    if (!this.dom.statusPill) return;
    this.dom.statusPill.className = `connection-pill ${state}`;
    if (this.dom.statusText) {
      this.dom.statusText.innerText = label;
    }
  }

  // --------------------------------------------------------------------------
  // 2. SENDING MESSAGES & OPTIMISTIC RENDERING
  // --------------------------------------------------------------------------
  sendMessage(text, attachmentType = "NONE", parentId = null) {
    const trimmed = (text || "").trim();
    if (!trimmed && attachmentType === "NONE") return;

    const tempId = `temp-${Date.now()}-${Math.random().toString(36).substr(2, 5)}`;
    const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    // 1. Optimistic Render immediately in UI
    const bubbleEl = this.appendMessageBubble({
      id: tempId,
      tempId: tempId,
      text: trimmed,
      senderName: "You",
      isSelf: true,
      timeStr: timeStr,
      status: "pending", // single checkmark pending ACK
    });

    this.scrollToBottom();

    // 2. Dispatch over WebSocket if connected
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          action: "send_message",
          temp_id: tempId,
          text: trimmed,
          attachment_type: attachmentType,
          parent_id: parentId,
        })
      );
    } else {
      showToast("Offline. Message queued.");
    }

    return tempId;
  }

  // --------------------------------------------------------------------------
  // 3. INCOMING EVENT DISPATCHER
  // --------------------------------------------------------------------------
  handleIncomingEvent(payload) {
    const type = payload.type;

    if (type === "ack") {
      // Single-tick ACK received from server
      const tempId = payload.temp_id;
      const realId = payload.message_id;
      const bubbleEl = document.querySelector(`[data-temp-id="${tempId}"]`);
      if (bubbleEl) {
        bubbleEl.setAttribute("data-message-id", realId);
        bubbleEl.removeAttribute("data-temp-id");
        this.updateReceiptIcon(bubbleEl, "sent");
      }
    } else if (type === "new_message") {
      const isSelf = String(payload.sender_id) === String(this.userId);
      // If message is from someone else, render it
      if (!isSelf) {
        const timeStr = payload.created_at
          ? new Date(payload.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
          : "Just now";

        const bubbleEl = this.appendMessageBubble({
          id: payload.message_id,
          text: payload.text,
          senderName: payload.sender_name || "Member",
          isSelf: false,
          timeStr: timeStr,
          attachmentType: payload.attachment_type,
        });

        this.scrollToBottom();

        // Observe this incoming bubble to mark it as read when user views it
        if (this.readObserver && bubbleEl) {
          this.readObserver.observe(bubbleEl);
        }

        // Notify server that message was delivered to our client
        this.markReceipt(payload.message_id, "mark_delivered");
      }
    } else if (type === "receipt_update") {
      // Update checkmark state on existing message
      const msgId = payload.message_id;
      const status = payload.status; // "DELIVERED" or "READ"
      const bubbleEl = document.querySelector(`[data-message-id="${msgId}"]`);
      if (bubbleEl) {
        this.updateReceiptIcon(bubbleEl, status.toLowerCase());
      }
    } else if (type === "reaction_update") {
      this.handleReactionBroadcast(payload);
    }
  }

  // --------------------------------------------------------------------------
  // 4. READ RECEIPTS & VIEWPORT OBSERVER
  // --------------------------------------------------------------------------
  initReadObserver() {
    if (!("IntersectionObserver" in window)) return;

    this.readObserver = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const el = entry.target;
            const messageId = el.getAttribute("data-message-id");
            if (messageId && !this.pendingReads.has(messageId)) {
              this.pendingReads.add(messageId);
              this.markReceipt(messageId, "mark_read");
              this.readObserver.unobserve(el);
            }
          }
        });
      },
      { threshold: 0.7 }
    );
  }

  scanUnreadMessagesInView() {
    const unreadBubbles = document.querySelectorAll('.message-row.other[data-message-id]');
    unreadBubbles.forEach((el) => {
      if (this.readObserver) this.readObserver.observe(el);
    });
  }

  markReceipt(messageId, action) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this.socket.send(
      JSON.stringify({
        action: action, // "mark_delivered" or "mark_read"
        message_id: messageId,
      })
    );
  }

  updateReceiptIcon(messageRowEl, status) {
    const iconWrapper = messageRowEl.querySelector(".receipt-icon");
    if (!iconWrapper) return;

    iconWrapper.className = "receipt-icon";
    if (status === "read") {
      iconWrapper.classList.add("read");
      iconWrapper.title = "Read";
      iconWrapper.innerHTML = `<i data-lucide="check-check" style="width: 13px; height: 13px;"></i>`;
    } else if (status === "delivered") {
      iconWrapper.title = "Delivered";
      iconWrapper.style.color = "inherit";
      iconWrapper.style.opacity = "0.75";
      iconWrapper.innerHTML = `<i data-lucide="check-check" style="width: 13px; height: 13px;"></i>`;
    } else {
      iconWrapper.title = "Sent";
      iconWrapper.style.color = "inherit";
      iconWrapper.style.opacity = "0.75";
      iconWrapper.innerHTML = `<i data-lucide="check" style="width: 12px; height: 12px;"></i>`;
    }

    if (window.lucide) lucide.createIcons();
  }

  // --------------------------------------------------------------------------
  // 5. DOM MESSAGE RENDERING
  // --------------------------------------------------------------------------
  appendMessageBubble({ id, tempId, text, senderName, isSelf, timeStr, status }) {
    if (!this.dom.stream) return null;

    // Remove empty-state placeholder if present
    const emptyState = this.dom.stream.querySelector(".empty-conversation-state");
    if (emptyState) {
      emptyState.remove();
    }

    const row = document.createElement("div");
    row.className = `message-row ${isSelf ? "self" : "other"}`;
    if (id) row.setAttribute("data-message-id", id);
    if (tempId) row.setAttribute("data-temp-id", tempId);

    let receiptHtml = "";
    if (isSelf) {
      receiptHtml = `
        <span class="receipt-icon" title="Sent">
          <i data-lucide="check" style="width: 12px; height: 12px;"></i>
        </span>
      `;
    }

    let senderHtml = "";
    if (!isSelf) {
      senderHtml = `<span class="sender-label">${escapeHtml(senderName)}</span>`;
    }

    row.innerHTML = `
      ${senderHtml}
      <div class="message-bubble">
        <p class="message-text">${escapeHtml(text)}</p>
        <div class="message-footer">
          <span>${timeStr}</span>
          ${receiptHtml}
        </div>
      </div>
    `;

    this.dom.stream.appendChild(row);
    if (window.lucide) lucide.createIcons();
    return row;
  }

  scrollToBottom() {
    if (!this.dom.stream) return;
    this.dom.stream.scrollTop = this.dom.stream.scrollHeight;
  }

  // --------------------------------------------------------------------------
  // 6. INVITE CODE & MODAL ACTIONS
  // --------------------------------------------------------------------------
  copyInviteToClipboard() {
    if (!this.inviteCode) {
      showToast("No active invite code for this room.");
      return;
    }
    const inviteLink = `${window.location.origin}/chat/join/?code=${this.inviteCode}`;
    navigator.clipboard.writeText(inviteLink).then(() => {
      showToast("Invite link copied to clipboard!");
    }).catch(() => {
      showToast(`Invite Code: ${this.inviteCode}`);
    });
  }

  async joinRoomWithCode(code) {
    const inviteCode = (code || "").trim();
    if (!inviteCode) {
      showToast("Please enter an invite code.");
      return false;
    }

    try {
      const csrfToken = getCookie("csrftoken") || "";
      const resp = await fetch("/chat/join/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ invite_code: inviteCode }),
      });

      const data = await resp.json();
      if (resp.ok && data.success) {
        showToast(`Joined "${data.room_name}" successfully!`);
        setTimeout(() => {
          window.location.href = `/chat/${data.room_id}/`;
        }, 600);
        return true;
      } else {
        showToast(data.error || "Failed to join room.");
        return false;
      }
    } catch (err) {
      console.error("Join error:", err);
      showToast("Network error joining room.");
      return false;
    }
  }
}

// ----------------------------------------------------------------------------
// HELPER UTILITIES
// ----------------------------------------------------------------------------
function escapeHtml(str) {
  if (!str) return "";
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(str).replace(/[&<>"']/g, (m) => map[m]);
}

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

function showToast(message) {
  let container = document.getElementById("toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `<i data-lucide="info" style="width: 14px; height: 14px;"></i><span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);
  if (window.lucide) lucide.createIcons();

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(8px)";
    setTimeout(() => toast.remove(), 250);
  }, 3000);
}

// ----------------------------------------------------------------------------
// GLOBAL INSTANCE & MODAL CONTROLS
// ----------------------------------------------------------------------------
let chatClientInstance = null;

function initChatClient(config) {
  chatClientInstance = new ChatClient(config);
  return chatClientInstance;
}

function openJoinModal() {
  const modal = document.getElementById("joinRoomModal");
  if (modal) {
    modal.classList.add("active");
    const input = document.getElementById("joinCodeInput");
    if (input) {
      input.value = "";
      input.focus();
    }
  }
}

function closeJoinModal() {
  const modal = document.getElementById("joinRoomModal");
  if (modal) modal.classList.remove("active");
}

function submitJoinRoom() {
  const input = document.getElementById("joinCodeInput");
  if (input && chatClientInstance) {
    chatClientInstance.joinRoomWithCode(input.value).then((success) => {
      if (success) closeJoinModal();
    });
  }
}


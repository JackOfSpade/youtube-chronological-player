/**
 * settings.js — Settings modal: channel management and search autocomplete.
 */

import { DOM, state, escapeHTML, showNotification, rebuildQueueIndex, findOldestUnwatchedIndex } from './state.js';
import { loadQueueData, renderQueue, updateResumeButton } from './queue.js';

// ── Public API ─────────────────────────────────────────────────────────────

let _lastSearchQuery = null;

export function openSettingsModal() {
    DOM.settingsModal.classList.remove('hidden');
    _fetchChannelConfig();
}

export function closeSettingsModal() {
    DOM.settingsModal.classList.add('hidden');
    if (state.searchTimeout) {
        clearTimeout(state.searchTimeout);
        state.searchTimeout = null;
    }
    _lastSearchQuery = null;
}

export function handleAddChannel() {
    const val = DOM.channelInput.value.trim();
    if (val && !state.settingsChannels.some(c => c.id === val)) {
        if (state.settingsChannels.length >= 100) {
            showNotification('Maximum of 100 channels allowed.');
            return;
        }
        state.settingsChannels.push({ id: val, name: val });
        DOM.channelInput.value = '';
        DOM.searchDropdown.classList.add('hidden');
        _renderChannels();
    }
}

export function handleSearchInput(e) {
    const q = e.target.value.trim();
    if (!q) { 
        DOM.searchDropdown.classList.add('hidden'); 
        if (state.searchTimeout) {
            clearTimeout(state.searchTimeout);
            state.searchTimeout = null;
        }
        return; 
    }
    if (state.searchTimeout) clearTimeout(state.searchTimeout);
    state.searchTimeout = setTimeout(() => _search(q), 800);
}

export async function saveChannels() {
    if (DOM.saveChannelsBtn.textContent === 'Saving...') return;
    DOM.saveChannelsBtn.textContent = 'Saving...';
    try {
        const res = await fetch('/api/channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channels: state.settingsChannels }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.message || 'Failed to save channels');
        }
        closeSettingsModal();
        loadQueueData(true);
    } catch (e) {
        console.error(e);
        showNotification(e.message || 'Failed to save channels');
    } finally {
        DOM.saveChannelsBtn.textContent = 'Save & Sync';
    }
}

// ── Private ────────────────────────────────────────────────────────────────

async function _fetchChannelConfig() {
    try {
        const res = await fetch('/api/config');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const raw = Array.isArray(data.channels) ? data.channels : [];
        state.settingsChannels = raw.map(c => {
            if (typeof c === 'string') return { id: c, name: c };
            if (!c || typeof c !== 'object') return { id: '', name: '' };
            return { id: String(c.id || c.name || ''), name: String(c.name || c.id || '') };
        }).filter(c => c.id);
        _renderChannels();
    } catch (e) {
        console.error(e);
    }
}

function _renderChannels() {
    DOM.channelList.innerHTML = '';
    if (state.settingsChannels.length === 0) {
        DOM.channelList.innerHTML = '<div class="queue-empty">No channels added.</div>';
        return;
    }

    const frag = document.createDocumentFragment();
    state.settingsChannels.forEach((ch, idx) => {
        const div = document.createElement('div');
        div.className = 'channel-item';
        div.innerHTML = `
            <span>${escapeHTML(ch.name || ch.id)}</span>
            <button class="remove-btn" data-index="${idx}">Remove</button>
        `;
        frag.appendChild(div);
    });
    DOM.channelList.appendChild(frag);
}

// Event delegation — set up once, works with dynamically rendered content
DOM.channelList.addEventListener('click', (e) => {
    const btn = e.target.closest('.remove-btn');
    if (!btn) return;
    const index = parseInt(btn.dataset.index, 10);
    state.settingsChannels.splice(index, 1);
    _renderChannels();
});


async function _search(query) {
    _lastSearchQuery = query;
    DOM.searchDropdown.innerHTML = '<div class="search-status">Searching...</div>';
    DOM.searchDropdown.classList.remove('hidden');

    try {
        const res = await fetch(`/api/search_channels?q=${encodeURIComponent(query)}`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const results = await res.json();
        if (_lastSearchQuery !== query) return;

        const existing = new Set(state.settingsChannels.map(c => c.id));
        const validResults = Array.isArray(results) ? results : [];
        const filtered = validResults.filter(ch => ch && !existing.has(ch.channelId));

        DOM.searchDropdown.innerHTML = '';
        if (filtered.length === 0) {
            DOM.searchDropdown.innerHTML = '<div class="search-status">No results found</div>';
            return;
        }

        const frag = document.createDocumentFragment();
        filtered.forEach(ch => {
            const item = document.createElement('div');
            item.className = 'search-result-item';
            item.innerHTML = `
                <img src="${escapeHTML(ch.thumbnail)}" class="search-result-thumb" loading="lazy" />
                <span class="search-result-title">${escapeHTML(ch.title)}</span>
            `;
            item.addEventListener('click', () => {
                if (!state.settingsChannels.some(c => c.id === ch.channelId)) {
                    if (state.settingsChannels.length >= 100) {
                        showNotification('Maximum of 100 channels allowed.');
                        return;
                    }
                    state.settingsChannels.push({ id: ch.channelId, name: ch.title });
                    _renderChannels();
                }
                DOM.channelInput.value = '';
                DOM.searchDropdown.classList.add('hidden');
            });
            frag.appendChild(item);
        });
        DOM.searchDropdown.appendChild(frag);
    } catch (e) {
        if (_lastSearchQuery !== query) return;
        console.error('Search failed', e);
        DOM.searchDropdown.innerHTML = '<div class="search-status search-error">Search failed</div>';
    }
}

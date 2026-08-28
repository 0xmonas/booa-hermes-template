const viewer = document.getElementById('log-viewer');
const evtSource = new EventSource('/logs/stream');

evtSource.onmessage = function(event) {
  viewer.textContent += event.data + '\n';
  viewer.scrollTop = viewer.scrollHeight;
};

evtSource.onerror = function() {
  viewer.textContent += '[connection lost — refresh to reconnect]\n';
};

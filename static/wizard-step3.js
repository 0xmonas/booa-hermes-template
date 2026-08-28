// Curated quick picks — ids and prices verified against the live OpenRouter
// catalog (2026-08). The full, always-current catalog is appended from
// /api/models below; this list is only the fallback and the "greatest hits".
const models = [
  { value: "anthropic/claude-haiku-4.5",  label: "Claude Haiku 4.5 (recommended)", info: "$1/$5 per MTok — fast, reliable tool use" },
  { value: "anthropic/claude-sonnet-5",   label: "Claude Sonnet 5",                info: "$2/$10 per MTok — fast + smart, 1M context" },
  { value: "anthropic/claude-sonnet-4.6", label: "Claude Sonnet 4.6",              info: "$3/$15 per MTok — 1M context" },
  { value: "anthropic/claude-opus-5",     label: "Claude Opus 5",                  info: "$5/$25 per MTok — most intelligent, 1M context" },
  { value: "anthropic/claude-opus-4.7",   label: "Claude Opus 4.7",                info: "$5/$25 per MTok — 1M context" },
  { value: "openai/gpt-5.6-luna",         label: "GPT-5.6 Luna",                   info: "$0.20/$1.20 per MTok — budget OpenAI" },
  { value: "openai/gpt-5.6-sol",          label: "GPT-5.6 Sol",                    info: "$2/$10 per MTok — fast OpenAI" },
  { value: "openai/gpt-5.6-terra",        label: "GPT-5.6 Terra",                  info: "$2/$12 per MTok — top OpenAI" },
  { value: "openai/gpt-5.4-mini",         label: "GPT-5.4 Mini",                   info: "$0.75/$4.50 per MTok" },
  { value: "deepseek/deepseek-v3.2",      label: "DeepSeek V3.2",                  info: "$0.27/$0.40 per MTok — budget with tool use" },
  { value: "google/gemini-2.5-pro",       label: "Gemini 2.5 Pro",                 info: "$1.25/$10 per MTok — 1M context" },
  { value: "google/gemini-2.5-flash",     label: "Gemini 2.5 Flash",               info: "$0.30/$2.50 per MTok — cheap + fast" },
  { value: "z-ai/glm-5.3-flash",          label: "GLM 5.3 Flash",                  info: "$0.08/$0.25 per MTok — cheapest tool-capable" },
  { value: "qwen/qwen3.8-flash",          label: "Qwen 3.8 Flash",                 info: "$0.15/$0.47 per MTok — 1M context" },
  { value: "meta-llama/llama-3.3-70b-instruct", label: "Llama 3.3 70B",            info: "$0.71/$0.71 per MTok — open-weight Meta" },
];

(function populate() {
  const select = document.getElementById('model');
  const info = document.getElementById('model-info');
  const infoById = {};

  function add(group, m) {
    const opt = document.createElement('option');
    opt.value = m.value;
    opt.textContent = m.label;
    (group || select).appendChild(opt);
    infoById[m.value] = m.info;
  }

  const curated = document.createElement('optgroup');
  curated.label = 'Recommended';
  models.forEach(m => add(curated, m));
  select.appendChild(curated);

  info.textContent = models[0].info;
  select.onchange = () => { info.textContent = infoById[select.value] || ''; };

  const fmt = v => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? '$' + (n * 1e6).toFixed(2) : '?';
  };

  fetch('/api/models').then(r => r.ok ? r.json() : Promise.reject()).then(d => {
    const seen = new Set(models.map(m => m.value));
    const rest = (d.models || []).filter(m => !seen.has(m.id));
    if (!rest.length) return;
    const group = document.createElement('optgroup');
    group.label = `Full OpenRouter catalog (${rest.length} more, newest first)`;
    rest.forEach(m => add(group, {
      value: m.id,
      label: m.name,
      info: `${fmt(m.prompt_price)}/${fmt(m.completion_price)} per MTok — ${m.context_length.toLocaleString()} context`,
    }));
    select.appendChild(group);
  }).catch(() => { /* curated list stands alone */ });
})();

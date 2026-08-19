// File: docs/assets/app.js
"use strict";

const DATA_PATH = "./data/character_usage.json";

const state = {
  data: null,
  characters: [],
  search: "",
  sort: "count-desc",
};

const elements = {
  status: document.querySelector("#status-message"),
  summary: document.querySelector("#summary"),
  rankingSection: document.querySelector("#ranking-section"),
  league: document.querySelector("#summary-league"),
  players: document.querySelector("#summary-players"),
  characters: document.querySelector("#summary-characters"),
  updated: document.querySelector("#summary-updated"),
  search: document.querySelector("#search-input"),
  sort: document.querySelector("#sort-select"),
  body: document.querySelector("#ranking-body"),
  resultCount: document.querySelector("#result-count"),
  csvButton: document.querySelector("#csv-button"),
  sourceLink: document.querySelector("#source-link"),
};

function normalizeSearchValue(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase("ja");
}

function formatInteger(value) {
  return new Intl.NumberFormat("ja-JP").format(Number(value) || 0);
}

function formatDate(value) {
  if (!value) {
    return "未集計";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return "不明";
  }

  return new Intl.DateTimeFormat("ja-JP", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Tokyo",
  }).format(date);
}

function isStale(value) {
  if (!value) {
    return false;
  }

  const updatedAt = new Date(value).getTime();

  if (Number.isNaN(updatedAt)) {
    return false;
  }

  const sevenDays = 7 * 24 * 60 * 60 * 1000;
  return Date.now() - updatedAt > sevenDays;
}

function setStatus(message, type = "normal") {
  elements.status.textContent = message;
  elements.status.className = "message";

  if (type === "error") {
    elements.status.classList.add("message-error");
  }

  if (type === "warning") {
    elements.status.classList.add("message-warning");
  }

  elements.status.hidden = false;
}

function hideStatus() {
  elements.status.hidden = true;
}

function validateData(data) {
  if (!data || typeof data !== "object") {
    throw new Error("集計データの形式が正しくありません。");
  }

  if (!Array.isArray(data.characters)) {
    throw new Error("キャラクター一覧が存在しません。");
  }

  for (const character of data.characters) {
    if (
      typeof character.name !== "string"
      || typeof character.count !== "number"
      || typeof character.rate !== "number"
    ) {
      throw new Error("キャラクターデータの形式が正しくありません。");
    }
  }
}

function renderSummary() {
  const data = state.data;

  elements.league.textContent = data.league || "-";
  elements.players.textContent =
    `${formatInteger(data.sampled_players)}人`;
  elements.characters.textContent =
    `${formatInteger(data.characters.length)}体`;
  elements.updated.textContent = formatDate(data.updated_at);

  if (data.source?.url) {
    elements.sourceLink.href = data.source.url;
  }

  if (data.source?.name) {
    elements.sourceLink.textContent = data.source.name;
  }

  elements.summary.hidden = false;
}

function getVisibleCharacters() {
  const search = normalizeSearchValue(state.search);

  const filtered = state.characters.filter((character) => {
    if (!search) {
      return true;
    }

    return normalizeSearchValue(character.name).includes(search);
  });

  filtered.sort((left, right) => {
    switch (state.sort) {
      case "count-asc":
        return (
          left.count - right.count
          || left.name.localeCompare(right.name, "ja")
        );

      case "name-asc":
        return left.name.localeCompare(right.name, "ja");

      case "count-desc":
      default:
        return (
          right.count - left.count
          || left.name.localeCompare(right.name, "ja")
        );
    }
  });

  return filtered;
}

function createCharacterImage(character) {
  const image = document.createElement("img");
  image.className = "character-image";
  image.loading = "lazy";
  image.decoding = "async";
  image.alt = "";
  image.width = 50;
  image.height = 50;

  if (character.image) {
    image.src = character.image;
  }

  image.addEventListener("error", () => {
    image.hidden = true;
  });

  return image;
}

function createRateCell(rate) {
  const cell = document.createElement("td");
  cell.className = "rate-cell";

  const value = document.createElement("span");
  value.className = "rate-value";
  value.textContent = `${rate.toFixed(1)}%`;

  const track = document.createElement("span");
  track.className = "rate-track";
  track.setAttribute("aria-hidden", "true");

  const bar = document.createElement("span");
  bar.className = "rate-bar";
  bar.style.width = `${Math.min(100, Math.max(0, rate))}%`;

  track.append(bar);
  cell.append(value, track);

  return cell;
}

function createTableRow(character, displayedRank) {
  const row = document.createElement("tr");

  const rankCell = document.createElement("td");
  rankCell.className = "rank-cell";
  rankCell.dataset.rank = String(displayedRank);
  rankCell.textContent = String(displayedRank);

  const characterCell = document.createElement("td");
  const characterLayout = document.createElement("div");
  characterLayout.className = "character-cell";

  const name = document.createElement("span");
  name.className = "character-name";
  name.textContent = character.name || "名称不明";

  characterLayout.append(
    createCharacterImage(character),
    name,
  );
  characterCell.append(characterLayout);

  const countCell = document.createElement("td");
  countCell.className = "number-cell";
  countCell.textContent = `${formatInteger(character.count)}人`;

  row.append(
    rankCell,
    characterCell,
    countCell,
    createRateCell(character.rate),
  );

  return row;
}

function renderTable() {
  const visibleCharacters = getVisibleCharacters();
  const fragment = document.createDocumentFragment();

  elements.body.replaceChildren();

  visibleCharacters.forEach((character, index) => {
    fragment.append(createTableRow(character, index + 1));
  });

  elements.body.append(fragment);
  elements.resultCount.textContent =
    `${formatInteger(visibleCharacters.length)}件を表示`;

  elements.rankingSection.hidden = false;
}

function escapeCsvCell(value) {
  const text = String(value ?? "");

  if (
    text.includes(",")
    || text.includes("\"")
    || text.includes("\n")
  ) {
    return `"${text.replaceAll("\"", "\"\"")}"`;
  }

  return text;
}

function downloadCsv() {
  const rows = [
    [
      "順位",
      "キャラクター",
      "採用数",
      "採用率",
      "リーグ",
      "集計人数",
      "更新日時",
    ],
  ];

  getVisibleCharacters().forEach((character, index) => {
    rows.push([
      index + 1,
      character.name,
      character.count,
      character.rate,
      state.data.league,
      state.data.sampled_players,
      state.data.updated_at,
    ]);
  });

  const csv = rows
    .map((row) => row.map(escapeCsvCell).join(","))
    .join("\r\n");

  const blob = new Blob(
    [`\uFEFF${csv}`],
    {
      type: "text/csv;charset=utf-8",
    },
  );

  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = objectUrl;
  anchor.download = "line-rangers-legend-character-usage.csv";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(objectUrl);
}

async function loadData() {
  try {
    const response = await fetch(
      `${DATA_PATH}?v=${Date.now()}`,
      {
        cache: "no-store",
      },
    );

    if (!response.ok) {
      throw new Error(
        `集計データを取得できませんでした。HTTP ${response.status}`,
      );
    }

    const data = await response.json();
    validateData(data);

    state.data = data;
    state.characters = [...data.characters];

    renderSummary();
    renderTable();

    if (data.characters.length === 0) {
      setStatus(
        "まだ集計データがありません。GitHub Actionsを手動実行してください。",
        "warning",
      );
      return;
    }

    if (isStale(data.updated_at)) {
      setStatus(
        "最終更新から7日以上経過しています。集計処理が停止している可能性があります。",
        "warning",
      );
      return;
    }

    hideStatus();
  } catch (error) {
    console.error(error);
    setStatus(
      error instanceof Error
        ? error.message
        : "集計データの読み込みに失敗しました。",
      "error",
    );
  }
}

elements.search.addEventListener("input", (event) => {
  state.search = event.currentTarget.value;
  renderTable();
});

elements.sort.addEventListener("change", (event) => {
  state.sort = event.currentTarget.value;
  renderTable();
});

elements.csvButton.addEventListener("click", downloadCsv);

loadData();

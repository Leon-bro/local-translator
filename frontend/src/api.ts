export type ChunkState = "p" | "e" | "a" | "f" | "b" | "x";

export interface Mode {
  id: string;
  label: string;
  description: string;
  pass1: string;
  pass2: string;
}

export interface FileInfo {
  hash: string;
  name: string;
  status: "new" | "extracted" | "pass1_done" | "pass2_done" | "rendered";
  mode: string;
  renderedMode: string;
  strip: string;
  total: number;
  done: number;
  flagged: number;
  second: number;
  errors: number;
  remainingSeconds: number;
  hasOutput: boolean;
  stale: boolean;
  outputMtime: number;
}

export interface JobState {
  running: boolean;
  label?: string;
  startedAt?: number;
  outcome?: "ok" | "failed" | "stopped";
  message?: string;
}

export interface AppState {
  job: JobState;
  files: FileInfo[];
  ollama: { up: boolean; models: string[]; loaded: string[] };
  config: { modes: Mode[]; defaultMode: string; keepNames: boolean };
}

export interface BlockPart {
  index: number;
  src: string;
  dst: string;
  model?: string;
  state: ChunkState;
  reasons?: string;
  alt?: string;
  altModel?: string;
}

export interface Block {
  id: number;
  type: "paragraph" | "heading" | "caption" | "image";
  page: number;
  text?: string;
  level?: number;
  sectionBreak?: boolean;
  asset?: string;
  parts?: BlockPart[];
}

export interface UserNote {
  index: number;
  text: string;
  quote: string;
  createdAt: number;
}

export interface Candidate {
  term: string;
  count: number;
  example: string;
}

export interface BookGroup {
  id: string;
  name: string;
  books: string[];
  terms: number;
}

export type CharacterChoice = "" | "m" | "f" | "x";

export interface Character {
  term: string;
  aliases: string[];
  person: boolean;
  guess: "" | "m" | "f";
  he: number;
  she: number;
  count: number;
  evidence: string[];
  choice: CharacterChoice;
}

export interface Term {
  source: string;
  target: string;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let message = `Сервер ответил ошибкой ${response.status}`;
    try {
      message = (await response.json()).error ?? message;
    } catch {
      /* в ответе не JSON: остаётся общее сообщение */
    }
    throw new Error(message);
  }
  return response.json();
}

const sendJSON = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const inGroup = (group: string) => `?group=${encodeURIComponent(group)}`;

export const api = {
  state: () => request<AppState>("/api/state"),
  blocks: (hash: string) => request<{ blocks: Block[] }>(`/api/files/${hash}/blocks`),
  notes: (hash: string) => request<{ notes: UserNote[] }>(`/api/files/${hash}/notes`),
  saveNote: (hash: string, index: number, text: string, quote: string) => request<{ notes: UserNote[] }>(`/api/files/${hash}/notes/${index}`, sendJSON("PUT", { text, quote })),
  deleteNote: (hash: string, index: number) => request<{ notes: UserNote[] }>(`/api/files/${hash}/notes/${index}`, { method: "DELETE" }),
  position: (hash: string) => request<{ index: number | null }>(`/api/files/${hash}/position`),
  // keepalive: запрос должен дойти, даже если вкладку уже закрывают.
  savePosition: (hash: string, index: number) => request<object>(`/api/files/${hash}/position`, { ...sendJSON("PUT", { index }), keepalive: true }),
  start: () => request<JobState>("/api/jobs", sendJSON("POST", { action: "start" })),
  translate: (hash: string) => request<JobState>("/api/jobs", sendJSON("POST", { action: "translate", hash })),
  rerender: (hash: string) => request<JobState>("/api/jobs", sendJSON("POST", { action: "rerender", hash })),
  stop: () => request<JobState>("/api/jobs/stop", { method: "POST" }),
  remove: (hash: string) => request<object>(`/api/files/${hash}`, { method: "DELETE" }),
  upload: (files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    return request<{ added: number; rejected: string[] | null }>("/api/upload", { method: "POST", body: form });
  },
  groups: () => request<{ groups: BookGroup[] }>("/api/groups"),
  createGroup: (name: string, copyFrom: string) => request<{ groups: BookGroup[] }>("/api/groups", sendJSON("POST", { name, copyFrom })),
  renameGroup: (id: string, name: string) => request<{ groups: BookGroup[] }>(`/api/groups/${id}`, sendJSON("PUT", { name })),
  deleteGroup: (id: string) => request<{ groups: BookGroup[] }>(`/api/groups/${id}`, { method: "DELETE" }),
  setBookGroup: (hash: string, group: string) => request<{ groups: BookGroup[] }>(`/api/files/${hash}/group`, sendJSON("PUT", { group })),
  glossary: (group: string) => request<{ terms: Term[]; comments: string[] | null; hints: number; hintsFile: string }>(`/api/glossary${inGroup(group)}`),
  importGlossary: (file: File, group: string) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ added: number; updated: number; hintsAdded: number; hintsUpdated: number; skipped: string[] }>(`/api/glossary/import${inGroup(group)}`, { method: "POST", body: form });
  },
  saveGlossary: (terms: Term[], comments: string[] | null, group: string) =>
    request<object>(`/api/glossary${inGroup(group)}`, sendJSON("PUT", { terms, comments })),
  addTerms: (terms: Term[], group: string) => request<{ added: number; updated: number }>(`/api/glossary/terms${inGroup(group)}`, sendJSON("POST", { terms })),
  /** Термин из читалки попадает в контекст группы, где книга лежит сейчас. */
  addBookTerms: (terms: Term[], hash: string) => request<{ added: number; updated: number }>(`/api/glossary/terms?book=${hash}`, sendJSON("POST", { terms })),
  candidates: (hash: string) => request<{ candidates: Candidate[]; known: Term[]; plain: string[]; keepNames: boolean; characters: Character[] }>(`/api/files/${hash}/candidates`),
  saveCharacters: (items: { term: string; value: CharacterChoice }[], group: string) => request<object>(`/api/glossary/characters${inGroup(group)}`, sendJSON("PUT", { items })),
  savePlainNames: (names: string[], group: string) => request<Record<string, never>>(`/api/glossary/plain-names${inGroup(group)}`, { method: "PUT", body: JSON.stringify({ names }) }),
  saveModes: (modes: Mode[], defaultMode: string, keepNames: boolean) => request<object>("/api/config", sendJSON("PUT", { modes, defaultMode, keepNames })),
  setMode: (hash: string, mode: string) => request<object>(`/api/files/${hash}/mode`, sendJSON("PUT", { mode })),
};

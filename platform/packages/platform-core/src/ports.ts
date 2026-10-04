import type { SiteFiles } from '../../schemas/src/index';

export interface Store {
  get<T>(ns: string, key: string): Promise<T | undefined>;
  put<T>(ns: string, key: string, value: T): Promise<void>;
  list<T>(ns: string): Promise<T[]>;
}
export type FileWrite = { path: string; content: unknown | null };
export interface RepoClient {
  getFiles(siteId: string, ref: string): Promise<SiteFiles>;
  createBranch(siteId: string, name: string, from: string): Promise<string>;
  commit(siteId: string, branch: string, message: string, writes: FileWrite[]): Promise<string>;
  openPr(siteId: string, branch: string, title: string, body: string): Promise<{ number: number }>;
  previewUrl(siteId: string, prNumber: number): Promise<string | null>;
  mergePr(siteId: string, prNumber: number): Promise<string>;
  closePr(siteId: string, prNumber: number): Promise<void>;
  revertCommit(siteId: string, sha: string, message: string): Promise<string>;
}

export type Block =
  | { type: 'text'; text: string; cache_control?: { type: 'ephemeral'; ttl?: '5m' | '1h' } }
  | { type: 'tool_use'; id: string; name: string; input: any }
  | { type: 'tool_result'; tool_use_id: string; content: string; is_error?: boolean };
export type Message = { role: 'user' | 'assistant'; content: string | Block[] };
export type ToolDef = { name: string; description: string; input_schema: Record<string, unknown>; strict?: boolean };
export type Usage = { input_tokens: number; output_tokens: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number };
export type LlmRequest = { system: string; messages: Message[]; tools: ToolDef[] };
export type LlmResponse = { content: Block[]; usage: Usage; model: string; stop_reason?: string };
export interface LlmClient { complete(req: LlmRequest): Promise<LlmResponse> }

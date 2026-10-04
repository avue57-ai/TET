import type { Block, LlmClient, LlmRequest, LlmResponse } from './ports';

export type Step = Block[] | ((req: LlmRequest, call: number) => Block[]);
/** Scripted model for tests: each call returns the next step. */
export class ScriptedLlm implements LlmClient {
  calls: LlmRequest[] = [];
  constructor(private steps: Step[], private model = 'claude-opus-5-5') {}
  async complete(req: LlmRequest): Promise<LlmResponse> {
    this.calls.push(structuredClone(req));
    const s = this.steps[Math.min(this.calls.length - 1, this.steps.length - 1)]!;
    const content = typeof s === 'function' ? s(req, this.calls.length) : s;
    return { content, model: this.model, usage: { input_tokens: 5000, output_tokens: 800, cache_read_input_tokens: 22000, cache_creation_input_tokens: 0 } };
  }
}
export const tool = (name: string, input: unknown, id = `t${Math.random().toString(36).slice(2, 8)}`): Block => ({ type: 'tool_use', id, name, input });

import type Anthropic from '@anthropic-ai/sdk';
import type { LlmClient, LlmRequest, LlmResponse } from './ports';

export const DEFAULT_MODEL = 'claude-opus-5-5';

/** Builds the Messages API request. Kept separate so the shape is unit-tested without network. */
export function buildRequest(req: LlmRequest, model = DEFAULT_MODEL, effort: 'low' | 'medium' | 'high' = 'medium') {
  return {
    model,
    max_tokens: 8000,
    // Frozen system prompt, cached for one hour.
    system: [{ type: 'text', text: req.system, cache_control: { type: 'ephemeral', ttl: '1h' } }],
    messages: req.messages,
    tools: req.tools.map((t) => ({ name: t.name, description: t.description, input_schema: t.input_schema, ...(t.strict ? { strict: true } : {}) })),
    // Opus 5.5: thinking is always on and forced tool_choice is rejected, so use adaptive thinking and auto tool choice.
    thinking: { type: 'adaptive' },
    output_config: { effort },
    tool_choice: { type: 'auto' },
  };
}

export class AnthropicLlmClient implements LlmClient {
  constructor(private client: Anthropic, private model = DEFAULT_MODEL, private effort: 'low' | 'medium' | 'high' = 'medium') {}
  async complete(req: LlmRequest): Promise<LlmResponse> {
    const r: any = await (this.client.messages as any).create(buildRequest(req, this.model, this.effort));
    return { content: r.content.filter((b: any) => b.type === 'text' || b.type === 'tool_use'), usage: r.usage, model: r.model, stop_reason: r.stop_reason };
  }
}

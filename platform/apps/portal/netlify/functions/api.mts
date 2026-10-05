import { runtime } from '../../src/runtime';

export default async (req: Request) => runtime().route(req);
export const config = { path: ['/api/*', '/assets/*'] };

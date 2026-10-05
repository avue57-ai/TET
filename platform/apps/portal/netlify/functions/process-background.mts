import { runtime } from '../../src/runtime';

// The "-background" suffix makes Netlify run this asynchronously (up to 15 minutes) and answer 202 at once.
export default async (req: Request) => runtime().background(req);

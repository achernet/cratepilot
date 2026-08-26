import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const publicSources = [
  readFileSync(new URL('../app/page.tsx', import.meta.url), 'utf8'),
  readFileSync(new URL('../app/case-study/page.tsx', import.meta.url), 'utf8'),
].join('\n');

test('public mode is fixture-only and contains no provider or acquisition transport', () => {
  assert.doesNotMatch(publicSources, /\bfetch\s*\(/);
  assert.doesNotMatch(publicSources, /XMLHttpRequest|CRATEPILOT_SPOTIFY_CLIENT_SECRET|child_process|spawn\s*\(/);
  assert.match(publicSources, /public site makes no provider calls and downloads nothing/i);
});

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { scoreTransition } from '../app/lib/scorer.ts';

const fixtures = JSON.parse(await readFile(new URL('../public/demo/scorer-fixtures.json', import.meta.url), 'utf8'));

for (const [index, fixture] of fixtures.entries()) {
  test(`browser scorer matches Python golden fixture ${index + 1}`, () => {
    const actual = scoreTransition(fixture.source, fixture.target);
    assert.equal(actual.total, Math.round(fixture.expected_total * 100));
    for (const component of ['tempo', 'harmony', 'energy', 'learned']) {
      assert.equal(actual[component], Math.round(fixture.expected_components[component] * 100));
    }
    assert.deepEqual(actual.explanation, fixture.expected_explanation);
  });
}

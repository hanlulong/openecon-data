import { describe, it, expect } from 'vitest';

// Basic smoke test to ensure the Vitest harness is wired up correctly.
describe('test harness', () => {
  it('runs a trivial assertion', () => {
    expect(Math.sqrt(4)).toBe(2);
  });
});

import { test } from "node:test";
import assert from "node:assert/strict";
import { binArrayIndexes, MAX_BINS_ONE_POSITION, requiredSides, toRaw, validateRange } from "../src/lp.ts";

test("toRaw converts human amounts exactly", () => {
  assert.equal(toRaw(0.1, 9), "100000000");
  assert.equal(toRaw("1.5", 6), "1500000");
  assert.equal(toRaw("0.0000001", 6), "0");          // below the token's precision
  assert.equal(toRaw(25, 0), "25");
  assert.throws(() => toRaw("-1", 6));
  assert.throws(() => toRaw("1e9", 6));
});

test("validateRange enforces order and the per-position bin limit", () => {
  assert.equal(validateRange(-10, 10, 0), null);
  assert.match(validateRange(10, -10, 0) ?? "", /max_bin_id/);
  assert.match(validateRange(0, MAX_BINS_ONE_POSITION, 0) ?? "", /at most/);
  assert.match(validateRange(5000, 5010, 0) ?? "", /too far/);
});

test("requiredSides follows DLMM rules", () => {
  assert.deepEqual(requiredSides(1, 20, 0), { x: true, y: false });     // above the price: token only
  assert.deepEqual(requiredSides(-20, -1, 0), { x: false, y: true });   // below the price: SOL only
  assert.deepEqual(requiredSides(-10, 10, 0), { x: true, y: true });    // around the price: both
});

test("binArrayIndexes covers negative bins", () => {
  assert.deepEqual(binArrayIndexes(-3296, -3288), [-48, -47]);   // -3296/70 = -47.1 -> -48, -3288/70 -> -47
  assert.deepEqual(binArrayIndexes(-1, 0), [-1, 0]);
  assert.deepEqual(binArrayIndexes(60, 75), [0, 1]);
});

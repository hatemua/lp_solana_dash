import { test } from "node:test";
import assert from "node:assert/strict";
import { binsForRange, serializeBins, toHuman } from "../src/bins.ts";

const big = (s: string) => ({ toString: () => s });

test("toHuman converts raw amounts with decimals", () => {
  assert.equal(toHuman(big("1500000000"), 9), 1.5);
  assert.equal(toHuman(big("5"), 6), 0.000005);
  assert.equal(toHuman(big("42"), 0), 42);
  assert.ok(Number.isNaN(toHuman(big("-1"), 6)));
});

test("serializeBins sorts by bin id and uses human units", () => {
  const out = serializeBins(
    [
      { binId: 5, price: "0.001", pricePerToken: "0.5", xAmount: big("2000000"), yAmount: big("0"), supply: big("10") },
      { binId: 4, price: "0.001", pricePerToken: "0.49", xAmount: big("0"), yAmount: big("3000000000"), supply: big("7") },
    ],
    6,
    9,
  );
  assert.deepEqual(out.map((b) => b.bin_id), [4, 5]);
  assert.equal(out[1].x, 2);
  assert.equal(out[0].y, 3);
  assert.equal(out[0].price, 0.49);
  assert.equal(out[1].supply, "10");
});

test("binsForRange matches the research doc (bin step 100: +30% = 27 bins)", () => {
  assert.equal(binsForRange(0.3, 100), 27);    // ln(1.3)/ln(1.01) = 26.37 -> 27 bins to cover the range
  assert.equal(binsForRange(0.3, 200), 14);
});

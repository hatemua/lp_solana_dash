import { test } from "node:test";
import assert from "node:assert/strict";
import { age, num, pct, price, shortAddr, usd } from "../src/lib/format.ts";
import { safeNext } from "../src/lib/auth.ts";
import { amountsFor, binsFor, priceAt, rangeFor } from "../src/lib/range.ts";

test("usd and pct formatting", () => {
  assert.equal(usd(1234567), "$1.23M");
  assert.equal(usd(12345), "$12.3k");
  assert.equal(usd(999), "$999");
  assert.equal(usd(null), "–");
  assert.equal(pct(3.456), "+3.5%");
  assert.equal(pct(-0.02, 1, true), "-2.0%");
  assert.equal(num(15300), "15.3k");
  assert.equal(age(0.5), "30m");
  assert.equal(age(72), "3d");
  assert.equal(shortAddr("So11111111111111111111111111111111111111112"), "So11…1112");
});

test("price shows enough significant digits for memecoins", () => {
  assert.equal(price(0.00001391), "0.00001391");
  assert.equal(price(109.5737), "109.5737");
});

test("binsFor matches the research doc", () => {
  assert.equal(binsFor(0.3, 100), 27);   // ln(1.3)/ln(1.01) = 26.4
  assert.equal(binsFor(0.3, 200), 14);
  assert.equal(binsFor(0, 100), 0);
});

test("priceAt is geometric in bins", () => {
  assert.ok(Math.abs(priceAt(1, 0, 10, 100) - 1.01 ** 10) < 1e-12);
  assert.ok(Math.abs(priceAt(2, 5, 4, 100) - 2 / 1.01) < 1e-12);
});

test("rangeFor puts each side where DLMM expects it", () => {
  // token is X: token side above the price, SOL below
  assert.deepEqual(rangeFor("token", 30, 100, 100, true), { min: 100, max: 127 });
  assert.deepEqual(rangeFor("sol", 30, 100, 100, true), { min: 73, max: 100 });
  // token is Y: reversed
  assert.deepEqual(rangeFor("token", 30, 100, 100, false), { min: 73, max: 100 });
  // 50/50 is symmetric and capped to fit one position
  const both = rangeFor("both", 200, 0, 100, true);
  assert.ok(both.max - both.min + 1 <= 69 && both.min === -both.max);
});

test("amountsFor splits 50/50 by value", () => {
  assert.deepEqual(amountsFor("sol", 0.1, true, 0.001), { x: 0, y: 0.1 });
  assert.deepEqual(amountsFor("token", 500, true, 0.001), { x: 500, y: 0 });
  const both = amountsFor("both", 0.2, true, 0.001);
  assert.equal(both.y, 0.1);
  assert.ok(Math.abs(both.x - 100) < 1e-9);       // 0.1 SOL of a token worth 0.001 SOL = 100 tokens
});

test("login redirect only goes to our OAuth authorize URL or a local path", () => {
  const api = "https://lp.api.joulity.com";
  const authz = `${api}/oauth/authorize?client_id=x&state=y`;
  assert.equal(safeNext(authz, api), authz);
  assert.equal(safeNext("/account", api), "/account");
  assert.equal(safeNext(null, api), "/account");
  assert.equal(safeNext("https://evil.example/oauth/authorize?x", api), "/account");
  assert.equal(safeNext("//evil.example", api), "/account");
  assert.equal(safeNext(`${api}.evil.example/oauth/authorize?x`, api), "/account");
  assert.equal(safeNext("javascript:alert(1)", api), "/account");
});

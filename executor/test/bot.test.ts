import { test } from "node:test";
import assert from "node:assert/strict";
import { Keypair } from "@solana/web3.js";
import { deriveKeypair, verifySigned, withdrawMessage } from "../src/bot.js";
import { createPrivateKey, sign } from "node:crypto";

const SEED = Buffer.alloc(32, 7);
const OWNER = "AYjvh7g89yaNpfshWK1dyRaHqMuSXTHDPeJF5K77NgqT";
const OTHER = "57JmG2uos4wGFtBuerGS2azHEPBBnp8FJRgeLmnx8uXV";

test("a bot wallet is the same every time, and bound to its id and owner", () => {
  const a = deriveKeypair(SEED, 1, OWNER).publicKey.toBase58();
  assert.equal(deriveKeypair(SEED, 1, OWNER).publicKey.toBase58(), a);
  assert.notEqual(deriveKeypair(SEED, 2, OWNER).publicKey.toBase58(), a);
  assert.notEqual(deriveKeypair(SEED, 1, OTHER).publicKey.toBase58(), a);
  assert.notEqual(deriveKeypair(Buffer.alloc(32, 8), 1, OWNER).publicKey.toBase58(), a);
});

test("signed messages verify only for the signer and the exact text", () => {
  const kp = Keypair.generate();
  const pkcs8 = Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), Buffer.from(kp.secretKey.slice(0, 32))]);
  const priv = createPrivateKey({ key: pkcs8, format: "der", type: "pkcs8" });
  const msg = withdrawMessage(3, 1_800_000_000);
  const signature = sign(null, Buffer.from(msg), priv).toString("base64");
  const pubkey = kp.publicKey.toBase58();
  assert.deepEqual(verifySigned({ pubkey, message: msg, signature }), { ok: true });
  assert.equal(verifySigned({ pubkey, message: withdrawMessage(4, 1_800_000_000), signature }).ok, false);
  assert.equal(verifySigned({ pubkey: OTHER, message: msg, signature }).ok, false);
  assert.equal(verifySigned({ pubkey, message: msg, signature: "x" }).ok, false);
});

import { describe, expect, it } from "vitest";
import { addTurn, MAX_TURNS } from "./recordedTurns";

describe("recorded turn list", () => {
  it("keeps one stable key per turn, newest first", () => {
    let turns = addTurn([], { file: "a.wav", nemotron: { investigation_id: "inv_A" } }, "k1");
    turns = addTurn(turns, { file: "b.wav", nemotron: { investigation_id: "inv_B" } }, "k2");
    expect(turns.map(t => t.key)).toEqual(["k2", "k1"]);
    expect(turns[0].nemotron.investigation_id).toBe("inv_B");  // the new turn owns its own investigation, not the slot's old one
    expect(turns[1].nemotron.investigation_id).toBe("inv_A");
  });
  it("caps the list", () => {
    let turns: { file: string; key: string }[] = [];
    for (let i = 0; i < 20; i++) turns = addTurn(turns, { file: `${i}.wav` }, `k${i}`);
    expect(turns).toHaveLength(MAX_TURNS);
    expect(new Set(turns.map(t => t.key)).size).toBe(MAX_TURNS);
  });
});

# "Explain this page"

Agreed 2026-11-10 (the user's request: "there are a lot of information
displayed on every tab. We need to add the capability where users can
chat and ask what display on the page, explanation of data in plain
English").

## How it works

Every tab gets the same question box, and every panel that shows data
gets an "Explain ▸" link. The page sends the server the tab and the panel
exactly as shown (its text, tables row by row, each chart's bar labels:
`shown()` in the page; step 2, so no number is formatted twice). The
server adds our own written meaning of each term, and the AI puts them in
plain English. The AI never reads Aternity and never counts, as with the
two chats.

```
click "Explain ▸" or type a question
        │  tab + the panel as shown
        ▼
server: the panel's own lines
      + our meaning of each term
        │
        ▼
AI words it in plain English
        │
        ▼
check: every number is on the page
   ├─ yes → the AI's answer
   └─ no  → our own written meanings
            of the panel's terms
```

Two kinds of answer:
- "What does this mean?": from the meanings we write (`explain.GLOSSARY`),
  reviewed by the user. The AI only rewords them.
- "What is this telling me now?": from the panel's current numbers, held
  to them as the chats are.

It does not read Aternity, does not guess at causes, and saves nothing.

## The user's choices

1. Where: "Explain ▸" on each data panel, plus the box at the bottom of
   each tab (Today and Software keep theirs).
2. What goes to the AI: the panel as shown, names included, to the
   self-hosted service (as the Today chat).
3. Model: the one the chats use (GPT on prod); Nemotron measured too.
4. Meanings: written from the code, reviewed by the user before step 2.

## Steps

1. Measure on stage: `uxcorrelate verify --ai` lines "explains a panel"
   (2 made-up panels of each tab) and "explains a long page" (4,000,
   12,000 and 40,000 characters), and the meanings for review.
2. Today and Care list.
3. Investigate and Slow start-ups, built by what step 1 showed (a whole
   investigation, or one section at a time).
4. Software, folded into its chat.

## Not on forms, the connection lights or dialogs

They have no numbers to explain.

## Measured on stage (2026-11-10)

Both models read a made-up page of 40,000 characters right (GPT in about
2 s, Nemotron in 3 to 9 s), so a whole investigation can be explained at
once (`explain.LINES_MAX`). GPT: 10 of 10 made-up panels; Nemotron 9 of
10 (a "twice" of its own: refused, as meant).

## Step 2 (Today, Care list)

"Explain ▸" at the bottom right of each panel opens one dialog: the
answer, who wrote it (the AI, or our meanings and why), and a box for a
question about that panel. The Care list also has "Ask about this page"
at its bottom (the whole tab as shown). Today keeps "Ask about today".
With no AI service set, the answer is our own written meanings.

## Steps 3 and 4 (Investigate, Slow start-ups, Software), 2026-11-11

The same "Explain ▸" is on every panel of these tabs (added by the page's
script). An investigation and the start-ups each have an "Ask about this
…" box for the whole tab as shown (a whole investigation fits: measured).
Software keeps "Ask about software" for counts; its panels have the link.

## Since 2026-11-12: one chat on every tab

"Ask about this page", "Ask about today" and "Ask about software" were
replaced by Ask anything at the bottom of every tab (`agent.py`, step
3). The page sends the tab as shown with each question; its `page`
look-up gives it to the AI with the same meanings (`explain.given`).
"Explain ▸" on each panel is unchanged.

# "Explain this page"

Agreed 2026-11-10 (the user's request: "there are a lot of information
displayed on every tab. We need to add the capability where users can
chat and ask what display on the page, explanation of data in plain
English").

## How it works

Every tab gets the same question box, and every panel that shows data
gets an "Explain ▸" link. The page tells the server which tab and panel.
The server takes the same numbers that panel shows, plus our own written
meaning of each term, and the AI puts them in plain English. The AI never
reads Aternity and never counts, as with the two chats.

```
click "Explain ▸" or type a question
        │  tab + panel + what is open
        ▼
server: the panel's own numbers
        (the same ones the page shows)
      + our meaning of each term
        │
        ▼
AI words it in plain English
        │
        ▼
check: every number is on the page
   ├─ yes → the AI's answer
   └─ no  → our own written sentence
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

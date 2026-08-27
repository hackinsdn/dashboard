# HackInSDN Dashboard — Frequently Asked Questions

This file is written for the RAG assistant: short, self-contained answers to the
questions users actually ask in the support chat. One heading per question, and
an answer that makes sense on its own — a chunk is retrieved without its
neighbours, so an answer that depends on the section above it will be quoted out
of context.

Keep it accurate: everything here is stated to users as fact, with this page
cited as the source. When the assistant refuses a question often (see the
refusal rate in the admin Assistant panel), the fix is usually a new entry here.

## What is a lab?

A lab is a ready-made network topology you can start from the dashboard and use
from your browser. Each lab has a description, a set of goals and a lab guide
with the steps to follow. Starting a lab creates your own private instance of
that topology — nobody else can see or use it.

## How do I start a lab?

Open **Labs** in the sidebar, pick a lab, and press the start button on the lab
page. The instance takes a little while to come up; the lab page shows its
status and, once it is running, the links to access each node.

## Why can't I see a lab that a colleague can see?

Labs can be restricted to specific groups. If a lab does not appear in your
list, you are probably not a member of the group it is restricted to. Ask the
lab's instructor or the support team to add you to the right group.

## How long does a lab instance last?

Lab instances expire automatically so the testbed stays available for everyone.
The lab instance page shows the expiration date. You are notified by e-mail
before an instance expires, and an expired instance is deleted after a
tolerance period.

## Can I extend the expiration date of my lab?

Yes. Open the lab instance and use the option to extend it. If you need more
time than the interface allows, open a support case and explain what you are
working on — administrators can extend an instance further.

## My lab is stuck starting / a node will not come up. What should I do?

First reload the lab instance page: status updates can lag behind. If a node is
still not running after a few minutes, delete the instance and start it again —
this resolves most transient failures. If it fails a second time, open a support
case and include the lab name and the time it happened, so the team can look at
the logs for that instance.

## How do I delete a lab instance?

Open the lab instance page and use the delete option. Deleting is immediate and
cannot be undone: anything you configured inside the lab is lost. Save the
files or configuration you want to keep before deleting.

## How do I change the language of the interface?

Use the globe menu in the top navigation bar and pick English or Português. The
choice is stored on your profile, so it follows you to your next session.

## How do I join a group?

Groups are managed by their owners. Open the **Groups** page to see the groups
you belong to. To join one, ask the group owner or an instructor to add you —
some groups also add you automatically when you sign in with an e-mail address
that is on the group's pre-approved list.

## What is a lab guide?

The lab guide is the step-by-step material for a lab: what to do, what to
observe, and the questions to answer. It is shown next to the running lab
instance so you can follow it while you work.

## Who can see my answers to a lab?

Your answers are visible to you and to the instructors of the group the lab
belongs to. They are used to give you feedback on your work.

## How do I report a bug or ask for a new feature?

Use the **Contact** page, which lists the project's support channels and issue
tracker. For anything specific to your account, your labs or your group, open a
support case from the chat widget instead — the team can see your account there.

## How do I talk to a person?

Open the chat widget and choose **Open a support case**, or press "Talk to a
human" while chatting with the assistant. Your conversation, including anything
you already asked the assistant, is sent to the support team.

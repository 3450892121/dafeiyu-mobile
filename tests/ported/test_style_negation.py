"""A negated image claim must not be cut into a broken sentence."""
from pathlib import Path
import unittest
from test_search_repair import load

STYLE = load(Path(__file__).resolve().parents[2] / 'runtime/core/style.py')


class StyleNegationTests(unittest.TestCase):
    def test_reported_sentence_shape_is_preserved_in_full(self):
        text='行，那就不发图了，光说话。等线理顺了，拍过来我看看。'
        self.assertEqual(STYLE.clean(text,self_growth_enabled=True),text)
        self.assertEqual(STYLE.style_violations(text,self_growth_enabled=True),[])

    def test_denials_refusals_and_conditions_keep_their_meaning(self):
        for text in ('以后不发表情包了，光说话。','我没有给你发图，是别人发的。',
                     '别说我发了图，我没发。','不是我发了图，是别人发的。',
                     '不用给你发图，直接说。','我不能给你发图，只能说说。',
                     '如果我发了图，你再看看。','要不要我给你发张图？',
                     '（我没有给你发图）这句只是说明。'):
            with self.subTest(text=text):
                self.assertEqual(STYLE.strip_sticker_claims(text),text)
                self.assertFalse(any(v['type']=='sticker_claim' for v in STYLE.style_violations(text,True)))

    def test_actual_claims_and_internal_markers_still_do_not_pass_as_prose(self):
        for text in ('给你发了张图。','图已经发过去了。','别急，图发你了。',
                     '我不难受，给你发图了。','[表情:开心]'):
            with self.subTest(text=text):
                self.assertNotEqual(STYLE.strip_sticker_claims(text),text)
                self.assertTrue(any(v['type']=='sticker_claim' for v in STYLE.style_violations(text,True)))


if __name__=='__main__':unittest.main()

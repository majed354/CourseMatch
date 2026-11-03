"""CPU-only geometry checks; no OCR, row-count inference or model invocation."""
import io
from pathlib import Path
import unittest
from PIL import Image, ImageDraw
from scripts.equivalency.image_regions import detect_table_regions

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'output/image-intake-trials-20261103/tafsir-topics.png'

def encoded(image):
    out = io.BytesIO(); image.save(out, format='PNG'); return out.getvalue()

def draw_table(draw, x0, y0, x1, body_end, rows):
    draw.rectangle((x0,y0,x1-1,y0+29),fill=(75,60,142))
    for index in range(rows):
        y=y0+30+(body_end-y0-30)*index//rows
        draw.line((x0,y,x1-1,y),fill='black',width=1)
    draw.rectangle((x0,body_end,x1-1,body_end+13),fill=(80,182,194))

class ImageRegionTests(unittest.TestCase):
    def test_two_colored_tables_with_unrelated_row_counts_and_footer_inclusion(self):
        im=Image.new('RGB',(600,800),'white');d=ImageDraw.Draw(im)
        draw_table(d,60,60,540,320,7);draw_table(d,60,440,540,700,4)
        regions=detect_table_regions(encoded(im));self.assertEqual(len(regions),2)
        for region,top,end in zip(regions,[60,440],[334,714]):
            self.assertLessEqual(region['bboxpixel'][0],60);self.assertGreaterEqual(region['bboxpixel'][2],540)
            self.assertLessEqual(region['bboxpixel'][1],top);self.assertGreaterEqual(region['bboxpixel'][3],end)
            self.assertEqual(region['method'],'saturation_wide_header_color_footer')
        self.assertLess(regions[0]['bboxpixel'][3],regions[1]['header_bbox'][1])
        self.assertEqual(regions,detect_table_regions(encoded(im)))

    def test_next_header_is_a_boundary_and_not_included(self):
        im=Image.new('RGB',(600,600),'white');d=ImageDraw.Draw(im)
        d.rectangle((50,40,549,69),fill=(75,60,142));d.rectangle((50,350,549,379),fill=(75,60,142))
        d.line((50,100,549,100),fill='black')
        regions=detect_table_regions(encoded(im));self.assertEqual(len(regions),1)
        self.assertLessEqual(regions[0]['bboxpixel'][3],350)
        self.assertEqual(regions[0]['method'],'saturation_wide_header_next_header')

    def test_blank_monochrome_and_unclosed_header_are_not_guessed(self):
        for kind in ['blank','unclosed']:
            im=Image.new('RGB',(500,500),'white');d=ImageDraw.Draw(im)
            if kind=='grid':
                for y in range(50,450,40):d.line((40,y,460,y),fill='black',width=2)
                for x in [40,200,460]:d.line((x,50,x,410),fill='black',width=2)
            if kind=='unclosed':d.rectangle((40,30,460,60),fill=(75,60,142))
            with self.subTest(kind=kind):self.assertEqual(detect_table_regions(encoded(im)),[])
        self.assertEqual(detect_table_regions(b'not an image'),[])

    def test_monochrome_verified_grid_keeps_full_image_instead_of_a_model_crop(self):
        im=Image.new('RGB',(500,500),'white');d=ImageDraw.Draw(im)
        for y in range(50,450,40):d.line((40,y,460,y),fill='black',width=2)
        for x in [40,200,460]:d.line((x,50,x,410),fill='black',width=2)
        regions=detect_table_regions(encoded(im))
        self.assertEqual(regions,[{'id':'table-1','bboxpixel':[0,0,500,500],
                                  'header_bbox':None,'method':'full_image_verified_black_grid'}])

    def test_horizontal_rules_without_vertical_borders_do_not_license_fallback(self):
        im=Image.new('RGB',(500,500),'white');d=ImageDraw.Draw(im)
        for y in [50,120,190,260,330]:d.line((40,y,460,y),fill='black',width=2)
        self.assertEqual(detect_table_regions(encoded(im)),[])

    def test_narrow_colored_decoration_is_not_a_table(self):
        im=Image.new('RGB',(600,400),'white');d=ImageDraw.Draw(im)
        d.rectangle((20,30,150,70),fill=(75,60,142));d.rectangle((20,200,150,215),fill=(80,182,194))
        self.assertEqual(detect_table_regions(encoded(im)),[])

    @unittest.skipUnless(FIXTURE.exists(),'private real image fixture unavailable')
    def test_real_fixture_full_topic_geometry_without_assessment_leak(self):
        image=Image.open(FIXTURE);self.assertEqual(image.size,(1191,1684))
        regions=detect_table_regions(FIXTURE.read_bytes());self.assertGreaterEqual(len(regions),1)
        first=regions[0];x0,y0,x1,y1=first['bboxpixel']
        self.assertLessEqual(x0,115);self.assertGreaterEqual(x1,1076)
        self.assertLessEqual(y0,178);self.assertGreaterEqual(y1,1121);self.assertLess(y1,1198)
        # Known printed geometry landmarks (not OCR/semantic row detection):
        # first body, all later lesson cells, both review cells and total band.
        for y in [280,435,482,530,578,625,675,722,770,817,865,910,961,1007,1054,1100]:
            self.assertTrue(y0<=y<y1)
        self.assertFalse(y0<=1230<y1)  # separate assessment header
        hx0,hy0,hx1,hy1=first['header_bbox']
        self.assertLessEqual(hy0,179);self.assertGreaterEqual(hy1,262)
        self.assertLessEqual(hx0,116);self.assertGreaterEqual(hx1,1075)
        self.assertEqual(image.crop(first['bboxpixel']).width,x1-x0)

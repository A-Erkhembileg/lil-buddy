"""Protect supplied character detail, transparent coats and complete walk clips."""
import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import desktop_cat as pet


class CharacterAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_original_character_detail_survives_in_native_frames(self):
        for animal in ("Cat", "Dog", "Fox"):
            with self.subTest(animal=animal):
                image = pet.animal_frame_image(animal, "rest", 0)
                self.assertEqual((image.width(), image.height()), (400, 320))
                colors = {image.pixelColor(x,y).name() for y in range(image.height())
                          for x in range(image.width()) if image.pixelColor(x,y).alpha()>200}
                self.assertGreater(len(colors), 200)
                self.assertEqual(image.pixelColor(0,0).alpha(), 0)

    def test_every_supplied_walking_frame_is_used_and_distinct(self):
        for animal,count in (("Cat",16),("Dog",16),("Fox",16)):
            frames = [pet.DRAWERS[animal]("walk",i,1).toImage() for i in range(count)]
            with self.subTest(animal=animal):
                self.assertEqual(len({bytes(im.constBits().asstring(im.sizeInBytes())) for im in frames}), count)
                self.assertEqual(pet.DRAWERS[animal]("walk",count,1).toImage(), frames[0])

    def test_large_render_samples_original_art_instead_of_enlarging_small_sprite(self):
        for animal in ("Cat","Dog","Fox"):
            small = pet.DRAWERS[animal]("rest",0,1).toImage()
            large = pet.DRAWERS[animal]("rest",0,1,target_size=(160,128)).toImage()
            with self.subTest(animal=animal):
                self.assertEqual((large.width(),large.height()),(160,128))
                self.assertNotEqual(large,small.scaled(160,128,Qt.AspectRatioMode.IgnoreAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))

    def test_husky_white_coat_survives_background_transparency(self):
        image = pet.animal_frame_image("Dog","rest",0)
        white = sum(1 for y in range(image.height()) for x in range(image.width())
                    if image.pixelColor(x,y).alpha()>220 and
                    min(image.pixelColor(x,y).red(),image.pixelColor(x,y).green(),image.pixelColor(x,y).blue())>220)
        self.assertGreater(white,1000)
        self.assertTrue(all(image.pixelColor(0,y).alpha()==0 for y in range(image.height())))


if __name__ == "__main__":
    unittest.main()

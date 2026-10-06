# Образы SD-карт

Сюда кладутся .img для записи на SD-карту бортового Raspberry Pi.
Пути и имена задаются в `drones.cfg` (ключ `images` у дрона), например:

    images = ROS1:images/obrik_ros1.img, ROS2:images/obrik_ros2.img

Файлы .img в git не хранятся (большие) — они лежат локально на ноутбуке.

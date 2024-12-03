import os
os.environ["KERAS_BACKEND"] = "tensorflow"

import tensorflow as tf
import numpy as np
from tensorflow.keras.losses import BinaryCrossentropy
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.metrics import Mean
from imageio.v2 import imread, imwrite
from models import steganogan_encoder_dense_model, steganogan_decoder_dense_model, steganogan_critic_model
from utils import text_to_bits, bits_to_text


class KerasSteganoGAN(tf.keras.Model):
  def __init__(self, encoder=None, decoder=None, critic=None, data_depth=1):
    super(KerasSteganoGAN, self).__init__()
    
    self.data_depth = data_depth

    self.encoder = encoder or steganogan_encoder_dense_model(data_depth)
    self.decoder = decoder or steganogan_decoder_dense_model(data_depth)
    self.critic  = critic or steganogan_critic_model()

    self.encoder_decoder_total_loss_tracker = Mean(name="encoder_decoder_total_loss")   
    self.critic_loss_tracker = Mean(name="critic_loss")
    self.similarity_loss_tracker = Mean(name="similarity_loss")
    self.decoder_loss_tracker = Mean(name="decoder_loss")
    self.decoder_accuracy_tracker = Mean(name="decoder_accuracy")
    self.realism_loss_tracker = Mean(name="realism_loss")
    self.psnr_tracker = Mean(name="psnr")
    self.ssim_tracker = Mean(name="ssim")
    self.rs_bpp_tracker = Mean(name="rs_bpp")

  @property
  def metrics(self):
    return [
      self.encoder_decoder_total_loss_tracker,
      self.critic_loss_tracker,
      self.similarity_loss_tracker,
      self.decoder_loss_tracker,
      self.decoder_accuracy_tracker,
      self.realism_loss_tracker,
      self.psnr_tracker,
      self.ssim_tracker,
      self.rs_bpp_tracker
    ]

  def models_summary(self):
    self.critic.summary()
    self.encoder.summary()
    self.decoder.summary()

  def compile(self, encoder_optimizer, decoder_optimizer, critic_optimizer, loss_fn):
    super(KerasSteganoGAN, self).compile()
    self.encoder_optimizer = encoder_optimizer or Adam(learning_rate=1e-4, beta_1=0.5)
    self.decoder_optimizer = decoder_optimizer or Adam(learning_rate=1e-4, beta_1=0.5)
    self.critic_optimizer  = critic_optimizer or Adam(learning_rate=1e-4, beta_1=0.5)
    self.loss_fn           = loss_fn or BinaryCrossentropy(from_logits=False)

  @tf.function 
  def call(self, inputs):
    cover_image, message = inputs
    
    stego_image = self.encoder([cover_image, message])
    recovered_message = self.decoder(stego_image)

    return stego_image, recovered_message

  @tf.function
  def critic_loss(self, cover_image, stego_image):
    cover_critic_score = self.critic(cover_image)
    stego_critic_score = self.critic(stego_image)
    return cover_critic_score - stego_critic_score

  @tf.function
  def endoder_decoder_loss(self, cover_image, stego_image, message, recovered_message):
    similarity_loss = tf.reduce_mean(tf.square(cover_image - stego_image))
    decoder_loss = self.loss_fn(message, recovered_message)
    realism_loss = self.critic(stego_image)

    total_loss = similarity_loss + decoder_loss + realism_loss

    return total_loss, similarity_loss, decoder_loss, realism_loss

  @tf.function
  def decoder_accuracy(self, message, recovered_message):
    binary_payload = tf.greater_equal(message, 0.5)
    binary_decoded = tf.greater_equal(recovered_message, 0.5)

    equal_elements = tf.equal(binary_payload, binary_decoded)
    casted_elements = tf.cast(equal_elements, tf.float32)
    decoder_accuracy = tf.reduce_mean(casted_elements)

    return decoder_accuracy


  @tf.function
  def train_step(self, data):
    cover_image, message = data

    with tf.GradientTape() as encoder_tape, tf.GradientTape() as decoder_tape, tf.GradientTape() as critic_tape:
      stego_image = self.encoder([cover_image, message])
      recovered_message = self.decoder(stego_image)

      encoder_decoder_total_loss, similarity_loss, decoder_loss, realism_loss = self.endoder_decoder_loss(cover_image, stego_image, message, recovered_message)
      decoder_accuracy = self.decoder_accuracy(message, recovered_message)
      critic_loss = self.critic_loss(cover_image, stego_image)

    encoder_grads = encoder_tape.gradient(encoder_decoder_total_loss, self.encoder.trainable_variables)
    decoder_grads = decoder_tape.gradient(encoder_decoder_total_loss, self.decoder.trainable_variables)
    critic_grads = critic_tape.gradient(critic_loss, self.critic.trainable_variables)

    self.encoder_optimizer.apply_gradients(zip(encoder_grads, self.encoder.trainable_variables))
    self.decoder_optimizer.apply_gradients(zip(decoder_grads, self.decoder.trainable_variables))
    self.critic_optimizer.apply_gradients(zip(critic_grads, self.critic.trainable_variables))

    for p in self.critic.trainable_variables:
      p.assign(tf.clip_by_value(p, -0.1, 0.1))

    self.encoder_decoder_total_loss_tracker.update_state(encoder_decoder_total_loss)
    self.critic_loss_tracker.update_state(critic_loss)
    self.similarity_loss_tracker.update_state(similarity_loss)
    self.decoder_loss_tracker.update_state(decoder_loss)
    self.decoder_accuracy_tracker.update_state(decoder_accuracy)
    self.realism_loss_tracker.update_state(realism_loss)
    self.psnr_tracker.update_state(tf.image.psnr(cover_image, stego_image, max_val=1.0))
    self.ssim_tracker.update_state(tf.image.ssim(cover_image, stego_image, max_val=1.0))
    self.rs_bpp_tracker.update_state(self.data_depth * (2 * decoder_accuracy - 1))

    return {
      'encoder_decoder_total_loss': self.encoder_decoder_total_loss_tracker.result(),
      'critic_loss': self.critic_loss_tracker.result(),
      'similarity_loss': self.similarity_loss_tracker.result(),
      'decoder_loss': self.decoder_loss_tracker.result(),
      'decoder_accuracy': self.decoder_accuracy_tracker.result(),
      'realism_loss': self.realism_loss_tracker.result(),
      'psnr': self.psnr_tracker.result(),
      'ssim': self.ssim_tracker.result(),
      'rs_bpp': self.rs_bpp_tracker.result()
    }

  @tf.function 
  def test_step(self, data):
    cover_image, message = data

    stego_image = self.encoder([cover_image, message])
    recovered_message = self.decoder(stego_image)

    encoder_decoder_total_loss, similarity_loss, decoder_loss, realism_loss = self.endoder_decoder_loss(cover_image, stego_image, message, recovered_message)
    decoder_accuracy = self.decoder_accuracy(message, recovered_message)
    critic_loss = self.critic_loss(cover_image, stego_image)

    self.encoder_decoder_total_loss_tracker.update_state(encoder_decoder_total_loss)
    self.critic_loss_tracker.update_state(critic_loss)
    self.similarity_loss_tracker.update_state(similarity_loss)
    self.decoder_loss_tracker.update_state(decoder_loss)
    self.decoder_accuracy_tracker.update_state(decoder_accuracy)
    self.realism_loss_tracker.update_state(realism_loss)
    self.psnr_tracker.update_state(tf.image.psnr(cover_image, stego_image, max_val=1.0))
    self.ssim_tracker.update_state(tf.image.ssim(cover_image, stego_image, max_val=1.0))
    self.rs_bpp_tracker.update_state(self.data_depth * (2 * decoder_accuracy - 1))

    return {
      'encoder_decoder_total_loss': self.encoder_decoder_total_loss_tracker.result(),
      'critic_loss': self.critic_loss_tracker.result(),
      'similarity_loss': self.similarity_loss_tracker.result(),
      'decoder_loss': self.decoder_loss_tracker.result(),
      'decoder_accuracy': self.decoder_accuracy_tracker.result(),
      'realism_loss': self.realism_loss_tracker.result(),
      'psnr': self.psnr_tracker.result(),
      'ssim': self.ssim_tracker.result(),
      'rs_bpp': self.rs_bpp_tracker.result()
    }

  def _image_to_tensor(self, image):
    image = tf.cast(image, tf.float32)
    image = tf.convert_to_tensor(image)
    image = (image / 127.5) - 1.0 # Normalize to [-1, 1]
    image = tf.expand_dims(image, axis=0)
    return image

  def _stego_tensor_to_image(self, stego_tensor):
    stego_image = tf.squeeze(stego_tensor)
    stego_image = (stego_image + 1.0) * 127.5 # Denormalize to [0, 255]
    stego_image = tf.cast(stego_image, tf.uint32)
    return stego_image

  def encode(self, cover_path, stego_path, message):
    cover = imread(cover_path)
    cover_tensor = self._image_to_tensor(cover)
    width, height, _ = cover.shape
    message_shape = (height, width, self.data_depth)

    message = text_to_bits(message, message_shape)
    message = np.reshape(message, (1, width, height, self.data_depth)) 
    message = tf.convert_to_tensor(message, dtype=tf.float32)

    stego_tensor = self.encoder([cover_tensor, message])
    stego_image = self._stego_tensor_to_image(stego_tensor)
    imwrite(stego_path, stego_image.numpy().astype(np.uint8))

  def decode(self, stego):
    stego = imread(stego)
    stego_tensor = self._image_to_tensor(stego)
    message_shape = (stego.shape[1], stego.shape[0], self.data_depth)

    message_tensor = self.decoder(stego_tensor)
    message_tensor = tf.round(message_tensor)
    message_tensor = tf.cast(message_tensor, tf.int8)

    message = bits_to_text(message_tensor, message_shape)
    return message
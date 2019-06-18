'''
Trains a context-encoder network to perform image inpainting as described in 'Context Encoders: Feature Learning by
Inpainting' by Pathak et al. Note that 'generator' and 'autoencoder' are the same thing and the terms used
interchangeably.
'''

import tensorflow as tf
import model
import load_data
import load_colors
import os
import matplotlib.pyplot as plt
import time
import click
from datetime import date

# Build models, define losses and optimizers
cross_entropy = tf.keras.losses.BinaryCrossentropy(reduction=tf.keras.losses.Reduction.SUM_OVER_BATCH_SIZE,
                                                   from_logits=True)

MSE = tf.keras.losses.MeanSquaredError(reduction=tf.keras.losses.Reduction.SUM_OVER_BATCH_SIZE)

'''
Defines the discriminator loss as the sum of
1. Binary cross entropy of its predictions of real centers against a tensor of all ones
2. Binary cross entropy of its predictions of generated centers against a tensor of all zeros

real_output: tensor of shape (batch_size, 1, 1, 1) - the discriminators predictions against real centers
fake_output: tensor of shape (batch_size, 1, 1, 1) - the discriminators predictions against centers generated by the
generator
'''


def discriminator_loss(real_output, fake_output):
    real_loss = cross_entropy(tf.ones_like(real_output), real_output)
    fake_loss = cross_entropy(tf.zeros_like(fake_output), fake_output)
    total_loss = real_loss + fake_loss
    return total_loss


'''
Defines the generator loss as the weighted sum of
1. Binary cross entropy of the discriminators predictions against a tensor of all ones (i.e. how well the generator
is tricking the discriminator.
2. Mean Squared error between the generated center and the real center


fake_output - tensor of shape (batch_size, 1, 1, 1) - the discriminators predictions against centers generated by the
generator
y_true - real center image array
y_pred - center image array generated by the autoencoder 
overlap - integer specifying the number of pixels to overlap the outside image with the center 
'''


def generator_loss(fake_output, y_true, y_pred, overlap, use_gpu, weight_l2=0.9, weight_adv=0.1):
    adv_loss = cross_entropy(tf.ones_like(fake_output), fake_output)
    if overlap != 0:
        if use_gpu:
            y_true_center = y_true[:, :, overlap:-overlap, overlap:-overlap]
            y_pred_center = y_pred[:, :, overlap:-overlap, overlap:-overlap]
            y_true_overlap_left = y_true[:, :, :overlap, :overlap]
            y_true_overlap_right = y_true[:, :, -overlap:, -overlap:]
            y_pred_overlap_left = y_pred[:, :, :overlap, :overlap]
            y_pred_overlap_right = y_pred[:, :, -overlap:, -overlap:]

        else:
            # if training is being done with an overlap, want 10x higher weight for loss in overlapped region
            y_true_center = y_true[:, overlap:-overlap, overlap:-overlap, :]
            y_pred_center = y_pred[:, overlap:-overlap, overlap:-overlap, :]
            y_true_overlap_left = y_true[:, :overlap, :overlap, :]
            y_true_overlap_right = y_true[:, -overlap:, -overlap:, :]
            y_pred_overlap_left = y_pred[:, :overlap, :overlap, :]
            y_pred_overlap_right = y_pred[:, -overlap:, -overlap:, :]

        center_loss = MSE(y_true_center, y_pred_center)
        overlap_loss = 10 * (
                MSE(y_true_overlap_left, y_pred_overlap_left) + MSE(y_true_overlap_right, y_pred_overlap_right))
        l2_loss = center_loss + overlap_loss
    else:

        l2_loss = MSE(y_true, y_pred)
    total_loss =  weight_l2 * l2_loss  + weight_adv * adv_loss
    return total_loss


'''
One forward step and, if training=True, one pass of backpropegation for both the generator and discriminator.
The tf.function decorator means that this function is 'compiled' into a tensorflow graph a la tensorflow 1.x 
to increase speed. 

images - tensor containing images to train with
real_centers - tensor containing the real image centers 
overlap - integer specifying the number of pixels to overlap the outside image with the center  
'''


@tf.function
def take_step(images, real_centers, overlap, generator, discriminator, use_gpu, generator_optimizer,
              discriminator_optimizer):

    # 'fDx' in paper, train the discriminator
    with tf.GradientTape() as disc_tape:
        real_output = discriminator(real_centers, training=True)
        generated_centers = generator(images, training=False)
        fake_output = discriminator(generated_centers, training=True)
        disc_loss = discriminator_loss(real_output, fake_output)

    discriminator_grads = disc_tape.gradient(disc_loss, discriminator.trainable_variables)
    discriminator_optimizer.apply_gradients(zip(discriminator_grads, discriminator.trainable_variables))

    # 'fGx' in paper, train the generator
    with tf.GradientTape() as gen_tape:
        generated_centers = generator(images, training=True)
        gen_loss = generator_loss(fake_output, real_centers, generated_centers, overlap, use_gpu)

    generator_grads = gen_tape.gradient(gen_loss, generator.trainable_variables)
    generator_optimizer.apply_gradients(zip(generator_grads, generator.trainable_variables))

    return gen_loss, disc_loss


'''
Calculates losses without training
'''


@tf.function
def calc_losses(images, real_centers, overlap, use_gpu, generator, discriminator):
    generated_centers = generator(images, training=False)
    real_output = discriminator(real_centers, training=False)
    fake_output = discriminator(generated_centers, training=False)

    disc_loss = discriminator_loss(real_output, fake_output)
    gen_loss = generator_loss(fake_output, real_centers, generated_centers, overlap, use_gpu)

    return gen_loss, disc_loss


def plot_loss(train_gen_loss, val_gen_loss, train_disc_loss, val_disc_loss, shuffle, save_dir):
    fig, (ax1, ax2) = plt.subplots(2, 1)
    ax1.plot(train_gen_loss, color='r', label='Training Loss')
    ax1.plot(val_gen_loss, color='b', label='Validation Loss')
    ax1.set_xlabel('Num. Epochs')
    ax1.set_ylabel('Loss')
    ax1.set_title('Generator Loss')
    ax1.legend()

    ax2.plot(train_disc_loss, color='r', label='Training Loss')
    ax2.plot(val_disc_loss, color='b', label='Validation Loss')
    ax2.set_xlabel('Num. Epochs')
    ax2.set_ylabel('Loss')
    ax2.set_title('Discriminator Loss')
    ax2.legend()

    fig.subplots_adjust(hspace=.5)
    if shuffle:
        filename = os.path.join(save_dir, 'Loss_history_shuffled_labels.png')
        plt.savefig(filename)
    else:
        filename = os.path.join(save_dir, 'Loss_history.png')
        plt.savefig(filename)
    plt.close()


def save_pictures(image_batch, center_batch, epoch, shuffle, use_gpu, save_dir, generator, type, num_pictures=1):
    gen_centers = (generator(image_batch, training=False) + 1) / 2
    center_batch = (center_batch + 1) / 2

    if use_gpu:
        center_batch = tf.transpose(center_batch, (0, 2, 3, 1))
        gen_centers = tf.transpose(gen_centers, (0, 2, 3, 1))

    for i in range(num_pictures):
        if shuffle:
            filename = os.path.join(save_dir, type + '_shuffle_epoch_{}_{}'.format(epoch, i) + '.png')
        else:
            filename = os.path.join(save_dir, type + '_epoch_{}_{}'.format(epoch, i) + '.png')
        fig, (ax1, ax2) = plt.subplots(2)
        fig.suptitle('Epoch: {}'.format(epoch))
        ax1.imshow(gen_centers[i, :, :, :])
        ax1.set_title('Generated Center')
        ax2.imshow(center_batch[i, :, :, :])
        ax2.set_title('Real Center')
        fig.subplots_adjust(hspace=.3)
        plt.savefig(filename)
        plt.close()


'''
Trains model, saves a model checkpoint every 5 epochs, and plots a graph of training and validation loss for both
the autoencoder and discriminator after training. 

train_dataset - tf dataset object containing the training images and centers
val_dataset - tf dataset object contain the validation images and centers
overlap - integer specifying the number of pixels to overlap the outside image with the center 
'''


def train(train_dataset, val_dataset, epochs, overlap, use_gpu, shuffle, lr, save_dir):
    generator = model.build_autoencoder(use_gpu)
    discriminator = model.build_discriminator(use_gpu)
    generator_optimizer = tf.keras.optimizers.Adam(lr * 10)
    discriminator_optimizer = tf.keras.optimizers.Adam(lr)
    checkpoint_dir = os.path.join(save_dir, 'training_checkpoints/')
    checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
    checkpoint = tf.train.Checkpoint(generator_optimizer=generator_optimizer,
                                     discriminator_optimizer=discriminator_optimizer,
                                     generator=generator,
                                     discriminator=discriminator)
    list_train_gen_loss = []
    list_train_disc_loss = []
    list_val_gen_loss = []
    list_val_disc_loss = []
    for epoch in range(epochs):
        start = time.time()
        train_gen_loss = 0
        train_disc_loss = 0
        val_gen_loss = 0
        val_disc_loss = 0
        count_train = 0
        count_val = 0
        for image_batch, center_batch in train_dataset:
            if use_gpu:
                image_batch = tf.transpose(image_batch, (0, 3, 1, 2))
                center_batch = tf.transpose(center_batch, (0, 3, 1, 2))
            gen_loss, disc_loss = take_step(image_batch,
                                            center_batch,
                                            overlap,
                                            generator,
                                            discriminator,
                                            use_gpu,
                                            generator_optimizer,
                                            discriminator_optimizer)
            train_gen_loss += gen_loss
            train_disc_loss += disc_loss
            count_train += 1
        if (epoch + 1) % 5 == 0 or epoch == 0:
            save_pictures(image_batch, center_batch, epoch, shuffle, use_gpu, save_dir, generator, 'train')
        for image_batch, center_batch in val_dataset:
            if use_gpu:
                image_batch = tf.transpose(image_batch, (0, 3, 1, 2))
                center_batch = tf.transpose(center_batch, (0, 3, 1, 2))
            gen_loss, disc_loss = calc_losses(image_batch, center_batch, overlap, use_gpu, generator, discriminator)
            val_gen_loss += gen_loss
            val_disc_loss += disc_loss
            count_val += 1

        if (epoch + 1) % 5 == 0 or epoch == 0:
            checkpoint.save(file_prefix=checkpoint_prefix)
            save_pictures(image_batch, center_batch, epoch, shuffle, use_gpu, save_dir, generator, 'val')

        train_gen_loss = train_gen_loss / count_train
        train_disc_loss = train_disc_loss / count_train
        val_gen_loss = val_gen_loss / count_val
        val_disc_loss = val_disc_loss / count_val

        list_train_gen_loss.append(train_gen_loss)
        list_train_disc_loss.append(train_disc_loss)
        list_val_gen_loss.append(val_gen_loss)
        list_val_disc_loss.append(val_disc_loss)

        print('Time for epoch {} is {} sec'.format(epoch + 1, time.time() - start))
        print('Generator - Training loss {} --- Validation loss {}'.format(train_gen_loss, val_gen_loss))
        print('Discriminator - Training loss {} --- Validation loss {} \n'.format(train_disc_loss, val_disc_loss))

    plot_loss(list_train_gen_loss, list_val_gen_loss, list_train_disc_loss, list_val_disc_loss, shuffle, save_dir)


def write_info_file(filename, train_data_path, val_data_path, overlap, batch_size, use_gpu, shuffle, epochs, lr,
                    run_number):
    info_list = []
    info_list.append('Context Encoder Hyperparameters: Run {} \n'.format(run_number))
    info_list.append('Training data found at: {} \n'.format(train_data_path))
    info_list.append('Validation data found at: {} \n'.format(val_data_path))
    info_list.append('Overlap: {} \n'.format(overlap))
    info_list.append('Batch Size: {} \n'.format(batch_size))
    info_list.append('Use GPU: {} \n'.format(use_gpu))
    info_list.append('Labels Shuffled: {} \n'.format(shuffle))
    info_list.append('Epochs: {} \n'.format(epochs))
    info_list.append('Learning Rate: {} \n'.format(lr))

    with open(filename, 'w') as f:
        f.writelines(info_list)


@click.command()
@click.argument('train_data_path', type=click.Path(exists=True, readable=True))
@click.argument('val_data_path', type=click.Path())
@click.option('--overlap', default=7, help='Size of overlap (in pixels) the predicted image in the real image')
@click.option('--batch_size', default=64)
@click.option('--use_gpu/--no_gpu', default=False)
@click.option('--epochs', default=50)
@click.option('--shuffle/--no_shuffle', default=False)
@click.option('--lr', default=2e-4, help='Learning rate for Adam optimizer')
@click.option('--run_number', default=1, help='ith run of the day')
def main(train_data_path, val_data_path, overlap, batch_size, use_gpu, shuffle, epochs, lr, run_number):
    today = str(date.today())
    run_number = '_' + str(run_number)
    if shuffle:
        save_dir = './Run_shuffled_' + today + run_number
    else:
        save_dir = './Run_' + today + run_number

    if os.path.exists(save_dir):
        ans = input(
            'The directory this run will write to already exists, would you like to overwrite this directory? ([y/n])')
        if ans == 'y':
            pass
        else:
            return
    else:
        os.makedirs(save_dir)
    info_file = os.path.join(save_dir, 'run_info.txt')
    write_info_file(info_file, train_data_path, val_data_path, overlap, batch_size, use_gpu, shuffle, epochs, lr,
                    run_number)

    #train_dataset = load_data.load_h5_to_dataset(train_data_path, overlap, shuffle)
    #train_dataset = load_colors.load_colors(10000, overlap)
    #train_dataset = train_dataset.batch(batch_size)

    #val_dataset = load_data.load_h5_to_dataset(val_data_path, overlap, shuffle)
    #val_dataset = load_colors.load_colors(2000, overlap)
    #val_dataset = val_dataset.batch(batch_size)

    train_dataset, val_dataset = load_data.load_simulated_data(train_data_path)
    train_dataset = train_dataset.batch(batch_size)
    val_dataset = val_dataset.batch(batch_size)

    train(train_dataset, val_dataset, epochs, overlap, use_gpu, shuffle, lr, save_dir)


if __name__ == '__main__':
    main()
